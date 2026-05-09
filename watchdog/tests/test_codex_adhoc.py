from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from watchdog.codex_adhoc import (
    CodexAdhocConfig,
    CodexAdhocQueue,
    build_operational_context,
    build_codex_args,
    build_codex_env,
    build_codex_prompt,
    cap_reply,
    command_available,
    resolve_codex_command,
    run_codex_adhoc,
    _runtime_summary,
)
from watchdog.config import load_config


class CodexPromptTests(unittest.TestCase):
    def test_prompt_sets_read_only_operational_scope(self) -> None:
        out = build_codex_prompt("why is health degraded?", "live_healthz={\"status\":\"ok\"}")
        self.assertIn("nt-watchdog", out)
        self.assertIn("read-only", out)
        self.assertIn("must not edit files", out)
        self.assertIn("must not call mutating HTTP endpoints", out)
        self.assertIn("Do not attempt localhost health/status commands", out)
        self.assertIn("Prefetched read-only operational context", out)
        self.assertIn("do not lead with sandbox limitations", out)
        self.assertIn("live_healthz", out)
        self.assertIn("User question: why is health degraded?", out)

    def test_args_use_read_only_sandbox_and_no_approval(self) -> None:
        cfg = CodexAdhocConfig(
            command="codex",
            workdir=r"C:\repo",
            data_dir=r"C:\repo\watchdog\state\codex_adhoc",
            timeout_sec=120,
            queue_max=2,
            max_reply_chars=3500,
        )
        args = build_codex_args(cfg, "out.txt")
        self.assertIn("--sandbox", args)
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
        self.assertIn("--ask-for-approval", args)
        self.assertEqual(args[args.index("--ask-for-approval") + 1], "never")
        self.assertLess(args.index("--ask-for-approval"), args.index("exec"))
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args)

    def test_ps1_command_prefers_sibling_cmd_shim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ps1 = Path(tmp) / "codex.ps1"
            cmd = Path(tmp) / "codex.cmd"
            ps1.write_text("pwsh", encoding="utf-8")
            cmd.write_text("cmd", encoding="utf-8")
            self.assertEqual(resolve_codex_command(str(ps1)), [str(cmd)])

    def test_ps1_command_falls_back_to_powershell_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ps1 = Path(tmp) / "codex.ps1"
            ps1.write_text("pwsh", encoding="utf-8")
            out = resolve_codex_command(str(ps1))
        self.assertEqual(out[:4], ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass"])
        self.assertIn("-File", out)

    def test_command_available_checks_resolved_command(self) -> None:
        with mock.patch("watchdog.codex_adhoc.resolve_codex_command", return_value=["codex.cmd"]):
            with mock.patch("watchdog.codex_adhoc._command_exists", return_value=True):
                with mock.patch("watchdog.codex_adhoc.subprocess.run") as run:
                    run.return_value.returncode = 0
                    self.assertTrue(command_available("codex.ps1"))
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["codex.cmd", "--version"])

    def test_env_omits_watchdog_and_telegram_secrets(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "PATH": "x",
                "TELEGRAM_BOT_TOKEN": "secret",
                "WATCHDOG_NT_PASSWORD": "secret",
                "CODEX_HOME": r"C:\codex",
            },
            clear=True,
        ):
            env = build_codex_env()
        self.assertEqual(env["PATH"], "x")
        self.assertEqual(env["CODEX_HOME"], r"C:\codex")
        self.assertNotIn("TELEGRAM_BOT_TOKEN", env)
        self.assertNotIn("WATCHDOG_NT_PASSWORD", env)

    def test_reply_cap(self) -> None:
        out = cap_reply("abcdef" * 20, 50)
        self.assertIn("truncated", out)
        self.assertLessEqual(len(out), 50)


class CodexQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_full_returns_message_without_running_job(self) -> None:
        cfg = CodexAdhocConfig(
            command="codex",
            workdir=".",
            data_dir=".",
            timeout_sec=120,
            queue_max=1,
            max_reply_chars=3500,
        )
        release = asyncio.Event()
        calls = []

        async def runner(question: str) -> str:
            calls.append(question)
            await release.wait()
            return "done"

        queue = CodexAdhocQueue(cfg, runner=runner)
        queue._available = True
        first = asyncio.create_task(queue.ask(42, "first"))
        await asyncio.sleep(0)
        second = await queue.ask(42, "second")
        release.set()
        self.assertEqual(await first, "done")
        self.assertIn("queue is full", second)
        self.assertEqual(calls, ["first"])


class CodexRunnerTests(unittest.TestCase):
    def test_run_uses_utf8_replacement_for_captured_output(self) -> None:
        cfg = CodexAdhocConfig(
            command="codex",
            workdir=".",
            data_dir=tempfile.mkdtemp(),
            timeout_sec=120,
            queue_max=2,
            max_reply_chars=3500,
        )
        completed = mock.Mock()
        completed.returncode = 0
        completed.stdout = "ok"
        completed.stderr = ""
        with mock.patch("watchdog.codex_adhoc.build_operational_context", return_value=""):
            with mock.patch("watchdog.codex_adhoc.subprocess.run", return_value=completed) as run:
                self.assertEqual(run_codex_adhoc(cfg, "test"), "ok")
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")

    def test_run_includes_operational_context_in_prompt(self) -> None:
        cfg = CodexAdhocConfig(
            command="codex",
            workdir=".",
            data_dir=tempfile.mkdtemp(),
            timeout_sec=120,
            queue_max=2,
            max_reply_chars=3500,
        )
        completed = mock.Mock()
        completed.returncode = 0
        completed.stdout = "ok"
        completed.stderr = ""
        with mock.patch("watchdog.codex_adhoc.subprocess.run", return_value=completed) as run:
            with mock.patch("watchdog.codex_adhoc.build_operational_context", return_value="live_healthz={status:ok}"):
                self.assertEqual(run_codex_adhoc(cfg, "test"), "ok")
        self.assertIn("live_healthz", run.call_args.kwargs["input"])

    def test_operational_context_reads_logs_and_redacts_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "watchdog" / "logs"
            logs.mkdir(parents=True)
            events = logs / "health_events.jsonl"
            events.write_text('{"kind":"cycle","health_status":"ok"}\n', encoding="utf-8")
            (logs / "watchdog.log").write_text(
                'warning: telegram_bot_token: "1234567890:abcdefghijklmnopqrstuvwxyz"\n',
                encoding="utf-8",
            )
            cfg = CodexAdhocConfig(
                command="codex",
                workdir=str(root),
                data_dir=str(root / "state"),
                timeout_sec=120,
                queue_max=2,
                max_reply_chars=3500,
                bridge_url="http://127.0.0.1:1",
                health_endpoint="/healthz",
                events_log_path=str(events),
            )
            out = build_operational_context(cfg)
        self.assertIn("latest_watchdog_events", out)
        self.assertIn("recent_error_like_log_lines", out)
        self.assertIn("<redacted>", out)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", out)

    def test_runtime_summary_keeps_account_and_strategy_context(self) -> None:
        payload = {
            "accounts": [{"name": "A", "connected": True}],
            "positions": [{"account": "A", "instrument": "ES"}],
            "strategy_runtime": {"strategies": [{"name": "S1"}, {"name": "S2"}]},
            "unrelated": "ignored",
        }
        out = _runtime_summary(payload)
        self.assertEqual(out["accounts"][0]["name"], "A")
        self.assertEqual(out["positions"][0]["instrument"], "ES")
        self.assertEqual(out["strategy_runtime"]["count"], 2)


class CodexConfigTests(unittest.TestCase):
    def test_load_config_resolves_adhoc_paths_and_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.yaml"
            cfg_path.write_text(
                "\n".join(
                    [
                        "telegram_adhoc_codex_workdir: work",
                        "telegram_adhoc_codex_data_dir: data/codex",
                    ]
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {
                    "TELEGRAM_ADHOC_CODEX_TIMEOUT_SEC": "7",
                    "TELEGRAM_ADHOC_CODEX_QUEUE_MAX": "3",
                    "TELEGRAM_ADHOC_CODEX_MAX_REPLY_CHARS": "999",
                },
                clear=False,
            ):
                cfg = load_config(str(cfg_path))
        self.assertEqual(cfg.telegram_adhoc_codex_timeout_sec, 7)
        self.assertEqual(cfg.telegram_adhoc_codex_queue_max, 3)
        self.assertEqual(cfg.telegram_adhoc_codex_max_reply_chars, 999)
        self.assertTrue(Path(cfg.telegram_adhoc_codex_workdir).is_absolute())
        self.assertTrue(Path(cfg.telegram_adhoc_codex_data_dir).is_absolute())


if __name__ == "__main__":
    unittest.main()
