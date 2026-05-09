from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Sequence


@dataclass
class CodexAdhocConfig:
    command: str
    workdir: str
    data_dir: str
    timeout_sec: int
    queue_max: int
    max_reply_chars: int


CodexRunner = Callable[[str], Awaitable[str]]


def build_codex_prompt(question: str) -> str:
    return "\n".join(
        [
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
            "Useful read-only commands:",
            "- rg for code/log search",
            "- python scripts/manage_watchdog.py status --json",
            "- GET http://localhost:8899/health",
            "- GET http://localhost:8899/healthz",
            "- Read NT logs under C:\\Users\\sshrestha\\Documents\\NinjaTrader 8\\log\\log.*.txt",
            "",
            f"User question: {question}",
        ]
    )


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
    prompt = build_codex_prompt(question)
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
