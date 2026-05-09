from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence


@dataclass
class CodexAdhocConfig:
    command: str
    workdir: str
    data_dir: str
    timeout_sec: int
    queue_max: int
    max_reply_chars: int
    bridge_url: str = "http://localhost:8899"
    health_endpoint: str = "/healthz"
    events_log_path: str = "watchdog/logs/health_events.jsonl"


CodexRunner = Callable[[str], Awaitable[str]]


def build_codex_prompt(question: str, operational_context: str = "") -> str:
    lines = [
            "You are answering operational questions for the nt-watchdog daemon.",
            "",
            "Hard limits:",
            "- You may read repo files, run read-only searches, inspect read-only SQLite data, and read NinjaTrader/watchdog logs.",
            "- You may use read-only HTTP GETs against localhost bridge health endpoints.",
            "- You must not edit files, recompile NinjaScript, restart processes, deploy, install packages, change startup tasks, or call recovery endpoints.",
            "- You must not call mutating HTTP endpoints, mutating SQL, or commands that place/cancel/modify trades.",
            "- Never print secrets, tokens, passwords, full environment files, or private auth material.",
            "- If config files are relevant, summarize only non-secret settings and redact credential values.",
            "- For SQLite, open databases read-only and keep queries non-mutating.",
            "- Keep the final answer concise and evidence-backed with timestamps, file paths, endpoint payloads, row ids, accounts, instruments, or log lines when relevant.",
            "",
            "Command guidance:",
            "- rg for code/log search",
            "- Do not attempt localhost health/status commands unless the prefetched context is missing or obviously stale.",
            "- The wrapper already prefetched live bridge health and recent log evidence outside your sandbox.",
            "- Read NT logs under C:\\Users\\sshrestha\\Documents\\NinjaTrader 8\\log\\log.*.txt",
            "",
    ]
    if operational_context:
        lines.extend(
            [
                "Prefetched read-only operational context:",
                operational_context,
                "",
                "Use the prefetched context first. If it answers the question, do not lead with sandbox limitations or failed command attempts.",
                "",
            ]
        )
    lines.append(f"User question: {question}")
    return "\n".join(lines)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    suffix = f"\n[truncated to {max_chars} chars]"
    return text[: max(0, max_chars - len(suffix))].rstrip() + suffix


def _redact_text(text: str) -> str:
    out = text
    key_value_pattern = (
        r'(?i)("?(?:telegram_bot_token|telegram_chat_id|nt_password|nt_username|password|token|secret)"?'
        r'\s*[:=]\s*)("[^"\r\n]*"|[^\s,\r\n]+)'
    )
    out = re.sub(key_value_pattern, lambda match: match.group(1) + "<redacted>", out)
    out = re.sub(r"\b\d{8,12}:[A-Za-z0-9_-]{20,}\b", "<redacted>", out)
    return out


def _safe_json(payload: Any, max_chars: int = 2500) -> str:
    try:
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    except TypeError:
        text = str(payload)
    return _truncate(_redact_text(text), max_chars)


def _tail_lines(path: Path, max_lines: int) -> List[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-max_lines:]


def _recent_log_files(paths: Iterable[Path], since: datetime) -> List[Path]:
    out: List[Path] = []
    for path in paths:
        try:
            if path.is_file() and datetime.fromtimestamp(path.stat().st_mtime).astimezone() >= since:
                out.append(path)
        except OSError:
            continue
    return sorted(out, key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)


def _matching_lines(paths: Iterable[Path], max_lines: int = 60) -> List[str]:
    pattern = re.compile(r"error|exception|traceback|failed|failure|warn|timeout", re.IGNORECASE)
    matches: List[str] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for idx, line in enumerate(lines, start=1):
            if pattern.search(line):
                matches.append(f"{path}:{idx}: {_redact_text(line)}")
    return matches[-max_lines:]


def build_operational_context(cfg: CodexAdhocConfig) -> str:
    now = datetime.now().astimezone()
    since = (now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1))
    workdir = Path(cfg.workdir)
    sections: List[str] = [
        f"- context_generated_local={now.isoformat(timespec='seconds')}",
        f"- recent_log_scan_since_local={since.isoformat(timespec='seconds')}",
    ]

    try:
        req = urllib.request.Request(
            cfg.bridge_url.rstrip("/") + cfg.health_endpoint,
            method="GET",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            health_payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        sections.append(f"- live_healthz={_safe_json(health_payload)}")
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as exc:
        sections.append(f"- live_healthz_error={type(exc).__name__}: {_redact_text(str(exc))}")

    events_path = Path(cfg.events_log_path)
    if not events_path.is_absolute():
        events_path = workdir / events_path
    event_lines = _tail_lines(events_path, 20)
    if event_lines:
        sections.append("- latest_watchdog_events:")
        sections.extend(f"  {line}" for line in event_lines[-20:])

    candidate_logs: List[Path] = []
    watchdog_logs = workdir / "watchdog" / "logs"
    if watchdog_logs.exists():
        candidate_logs.extend(_recent_log_files(watchdog_logs.glob("*.log"), since))
    nt_logs = Path.home() / "Documents" / "NinjaTrader 8" / "log"
    if nt_logs.exists():
        candidate_logs.extend(_recent_log_files(nt_logs.glob("log.*.txt"), since)[:5])
    matches = _matching_lines(candidate_logs)
    if matches:
        sections.append("- recent_error_like_log_lines:")
        sections.extend(f"  {line}" for line in matches)
    else:
        sections.append("- recent_error_like_log_lines: none found in recent watchdog/NT log files scanned")

    return _truncate("\n".join(sections), 12000)


def build_codex_args(cfg: CodexAdhocConfig, output_file: str) -> List[str]:
    return [
        "--ask-for-approval",
        "never",
        "exec",
        "--skip-git-repo-check",
        "--cd",
        cfg.workdir,
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--output-last-message",
        output_file,
        "-",
    ]


def build_codex_env() -> Dict[str, str]:
    allowed = [
        "PATH",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "CODEX_HOME",
        "LANG",
        "SYSTEMROOT",
        "COMSPEC",
        "TEMP",
        "TMP",
    ]
    env = {key: value for key in allowed if (value := os.environ.get(key))}
    env.setdefault("LANG", "C.UTF-8")
    return env


def resolve_codex_command(command: str) -> List[str]:
    """Return an executable argv prefix for Codex.

    npm on Windows installs `codex.ps1` and `codex.cmd` shims. Python cannot
    execute `.ps1` directly, so prefer the sibling `.cmd` shim when present.
    """
    path = Path(command)
    if path.suffix.lower() == ".ps1":
        cmd_path = path.with_suffix(".cmd")
        if cmd_path.exists():
            return [str(cmd_path)]
        return [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(path),
        ]
    return [command]


def cap_reply(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    suffix = f"\n\n[truncated: reply exceeded {max_chars} chars]"
    if len(suffix) >= max_chars:
        return suffix[:max_chars]
    keep = max(0, max_chars - len(suffix))
    return text[:keep].rstrip() + suffix


def _command_exists(argv_prefix: Sequence[str]) -> bool:
    command = argv_prefix[0]
    path = Path(command)
    if path.is_absolute() or path.parent != Path("."):
        return path.exists()
    return shutil.which(command) is not None


def command_available(command: str, timeout_sec: int = 5) -> bool:
    argv_prefix = resolve_codex_command(command)
    if not _command_exists(argv_prefix):
        return False
    try:
        proc = subprocess.run(
            [*argv_prefix, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_sec,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def run_codex_adhoc(cfg: CodexAdhocConfig, question: str) -> str:
    data_dir = Path(cfg.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    output_file = data_dir / f"{int(time.time() * 1000)}-{uuid.uuid4().hex}.txt"
    prompt = build_codex_prompt(question, build_operational_context(cfg))
    args = build_codex_args(cfg, str(output_file))
    try:
        try:
            proc = subprocess.run(
                [*resolve_codex_command(cfg.command), *args],
                input=prompt,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                cwd=cfg.workdir,
                env=build_codex_env(),
                timeout=max(1, int(cfg.timeout_sec)),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return f"Codex timed out after {int(cfg.timeout_sec)}s."
        except OSError as exc:
            return f"Codex failed to start: {exc}"

        if proc.returncode != 0:
            detail = " ".join((proc.stderr or proc.stdout or "").split())
            if detail:
                return f"Codex failed with exit={proc.returncode}: {cap_reply(detail, 1000)}"
            return f"Codex failed with exit={proc.returncode}."

        answer = ""
        try:
            answer = output_file.read_text(encoding="utf-8").strip()
        except OSError:
            answer = ""
        if not answer:
            answer = (proc.stdout or "").strip()
        return cap_reply(answer or "Codex completed without an answer.", cfg.max_reply_chars)
    finally:
        try:
            output_file.unlink(missing_ok=True)
        except OSError:
            pass


class CodexAdhocQueue:
    def __init__(
        self,
        cfg: CodexAdhocConfig,
        runner: Optional[CodexRunner] = None,
    ) -> None:
        self.cfg = cfg
        self._runner = runner or self._default_runner
        self._queue: List[asyncio.Future[str]] = []
        self._jobs: List[str] = []
        self._running = False
        self._available: Optional[bool] = None
        self._lock = asyncio.Lock()

    async def ask(self, user_id: int, text: str) -> str:
        del user_id
        async with self._lock:
            active_jobs = len(self._queue) + (1 if self._running else 0)
            if active_jobs >= max(1, int(self.cfg.queue_max)):
                return f"Codex queue is full ({self.cfg.queue_max}). Try again later."
            fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            self._queue.append(fut)
            self._jobs.append(text)
            if not self._running:
                asyncio.create_task(self._drain())
        return await fut

    async def check_available(self) -> bool:
        if self._available is None:
            self._available = await asyncio.to_thread(command_available, self.cfg.command)
        return self._available

    async def _default_runner(self, question: str) -> str:
        return await asyncio.to_thread(run_codex_adhoc, self.cfg, question)

    async def _drain(self) -> None:
        async with self._lock:
            if self._running:
                return
            self._running = True
        try:
            while True:
                async with self._lock:
                    if not self._queue:
                        return
                    fut = self._queue.pop(0)
                    question = self._jobs.pop(0)

                try:
                    if not await self.check_available():
                        result = (
                            f"Codex unavailable: '{self.cfg.command}' is not installed "
                            "or not authenticated for this service user."
                        )
                    else:
                        result = await self._runner(question)
                except Exception as exc:  # pragma: no cover - defensive
                    result = f"Codex failed: {exc!r}"
                if not fut.cancelled():
                    fut.set_result(result)
        finally:
            async with self._lock:
                self._running = False
