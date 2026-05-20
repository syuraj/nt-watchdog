from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from watchdog.config import WatchdogConfig
from watchdog.scheduled_log_scanner import ScheduledLogScanner, _parse_nt_log_time


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_message(self, message: str) -> bool:
        self.messages.append(message)
        return True


def _config(tmp: str) -> WatchdogConfig:
    return WatchdogConfig(
        telegram_enabled=True,
        telegram_bot_token="token",
        telegram_chat_id="chat",
        events_log_path=str(Path(tmp) / "watchdog" / "logs" / "health_events.jsonl"),
    )


def _nt_log_dir(home: Path) -> Path:
    path = home / "Documents" / "NinjaTrader 8" / "log"
    path.mkdir(parents=True)
    return path


class ScheduledLogScannerTests(unittest.TestCase):
    def test_parse_nt_log_time(self) -> None:
        parsed = _parse_nt_log_time(
            "2026-05-20 10:05:00:123|1|32|Order='x' New state='Rejected'"
        )

        self.assertEqual(parsed, datetime(2026, 5, 20, 10, 5, 0, 123000))

    def test_startup_offsets_prevent_replaying_existing_nt_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            nt_log_dir = _nt_log_dir(home)
            log_file = nt_log_dir / "log.20260520.00000.txt"
            log_file.write_text(
                "2026-05-20 09:55:00:000|1|32|Order='old/account' New state='Rejected'\n",
                encoding="utf-8",
            )
            scanner = ScheduledLogScanner(_config(tmp), FakeNotifier())  # type: ignore[arg-type]

            with mock.patch("watchdog.scheduled_log_scanner.Path.home", return_value=home):
                scanner._initialize_nt_log_offsets()
                with log_file.open("a", encoding="utf-8") as fh:
                    fh.write(
                        "2026-05-20 10:05:00:000|1|32|Order='new/account' New state='Rejected'\n"
                    )
                issues = scanner._scan_nt_logs(datetime(2026, 5, 20, 10, 10))

        self.assertEqual(len(issues), 1)
        self.assertIn("new/account", issues[0]["line"])

    def test_scan_nt_logs_ignores_old_timestamps_even_without_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            nt_log_dir = _nt_log_dir(home)
            (nt_log_dir / "log.20260430.00001.txt").write_text(
                "2026-04-30 18:00:00:155|1|32|Order='old/account' New state='Rejected'\n",
                encoding="utf-8",
            )
            scanner = ScheduledLogScanner(_config(tmp), FakeNotifier())  # type: ignore[arg-type]

            with mock.patch("watchdog.scheduled_log_scanner.Path.home", return_value=home):
                issues = scanner._scan_nt_logs(datetime(2026, 5, 20, 10, 10))

        self.assertEqual(issues, [])

    def test_scan_nt_logs_skips_en_txt_duplicates(self) -> None:
        line = "2026-05-20 10:05:00:000|1|32|Order='same/account' New state='Rejected'\n"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            nt_log_dir = _nt_log_dir(home)
            (nt_log_dir / "log.20260520.00000.txt").write_text(line, encoding="utf-8")
            (nt_log_dir / "log.20260520.00000.en.txt").write_text(line, encoding="utf-8")
            scanner = ScheduledLogScanner(_config(tmp), FakeNotifier())  # type: ignore[arg-type]

            with mock.patch("watchdog.scheduled_log_scanner.Path.home", return_value=home):
                issues = scanner._scan_nt_logs(datetime(2026, 5, 20, 10, 10))

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["file"], "log.20260520.00000.txt")

    def test_alert_header_uses_actual_recent_window(self) -> None:
        notifier = FakeNotifier()
        scanner = ScheduledLogScanner(_config("unused"), notifier)  # type: ignore[arg-type]

        scanner._send_alert(
            "Order rejected",
            [
                {
                    "source": "nt_log",
                    "file": "log.20260520.00000.txt",
                    "line": "2026-05-20 10:05:00:000|1|32|Order='x' New state='Rejected'",
                }
            ],
        )

        self.assertIn("last 20 min", notifier.messages[0])
        self.assertNotIn("last 15 min", notifier.messages[0])


if __name__ == "__main__":
    unittest.main()
