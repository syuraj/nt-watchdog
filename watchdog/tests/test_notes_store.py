from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from watchdog.config import WatchdogConfig, load_config
from watchdog.notes_store import NotesStore, format_notes


class NotesStoreTests(unittest.TestCase):
    def test_append_note_writes_jsonl_and_read_notes_returns_today(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = WatchdogConfig(notes_dir=str(Path(tmp) / "notes"), notes_max_chars=2000)
            store = NotesStore(
                cfg,
                now_provider=lambda: datetime(2026, 5, 20, 12, 30, tzinfo=timezone.utc),
            )

            item = store.append_note("watch NQ breakout after 10am", user_id=42)
            notes = store.read_notes(datetime(2026, 5, 20, tzinfo=timezone.utc).date())

        self.assertEqual(item["text"], "watch NQ breakout after 10am")
        self.assertEqual(item["user_id"], 42)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["source"], "telegram")

    def test_append_note_redacts_secrets_and_caps_length(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = WatchdogConfig(notes_dir=str(Path(tmp) / "notes"), notes_max_chars=40)
            store = NotesStore(
                cfg,
                now_provider=lambda: datetime(2026, 5, 20, 12, 30, tzinfo=timezone.utc),
            )

            item = store.append_note('telegram_bot_token: "1234567890:abcdefghijklmnopqrstuvwxyz" keep this long')

        self.assertIn("<redacted>", item["text"])
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", item["text"])
        self.assertLessEqual(len(item["text"]), 40)

    def test_append_note_uses_local_day_for_note_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = WatchdogConfig(notes_dir=str(Path(tmp) / "notes"), notes_max_chars=2000)
            eastern_evening = timezone(timedelta(hours=-4))
            store = NotesStore(
                cfg,
                now_provider=lambda: datetime(2026, 5, 19, 20, 30, tzinfo=eastern_evening),
            )

            store.append_note("after close note")

            notes_dir = Path(cfg.notes_dir)
            self.assertTrue((notes_dir / "2026-05-19.jsonl").exists())
            self.assertFalse((notes_dir / "2026-05-20.jsonl").exists())

    def test_format_notes(self) -> None:
        out = format_notes(
            [
                {
                    "time_utc": "2026-05-20T12:30:00Z",
                    "text": "review slippage after news",
                }
            ],
            label="today",
        )

        self.assertIn("Notes for today", out)
        self.assertIn("12:30Z", out)
        self.assertIn("review slippage", out)

    def test_load_config_resolves_notes_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.yaml"
            cfg_path.write_text(
                "\n".join(
                    [
                        "notes_enabled: false",
                        "notes_dir: runtime/notes",
                        "notes_max_chars: 100",
                    ]
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                "os.environ",
                {
                    "NOTES_ENABLED": "true",
                    "NOTES_MAX_CHARS": "75",
                },
                clear=False,
            ):
                cfg = load_config(str(cfg_path))

        self.assertTrue(cfg.notes_enabled)
        self.assertEqual(cfg.notes_max_chars, 75)
        self.assertTrue(Path(cfg.notes_dir).is_absolute())
        self.assertTrue(str(cfg.notes_dir).endswith(str(Path("runtime") / "notes")))


if __name__ == "__main__":
    unittest.main()
