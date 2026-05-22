from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from watchdog.config import WatchdogConfig, load_config
from watchdog.notes_store import NotesStore, extract_action_items, format_notes


class NotesStoreTests(unittest.TestCase):
    def test_append_note_writes_notes_md_and_read_markdown_notes_returns_today(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = WatchdogConfig(
                notes_dir=str(Path(tmp) / "notes"),
                notes_markdown_path=str(Path(tmp) / "notes.md"),
                notes_max_chars=2000,
            )
            store = NotesStore(
                cfg,
                now_provider=lambda: datetime(2026, 5, 20, 12, 30, tzinfo=timezone.utc),
            )

            item = store.append_note("watch NQ breakout after 10am", user_id=42)
            notes = store.read_markdown_notes(datetime(2026, 5, 20, tzinfo=timezone.utc).date())
            text = Path(cfg.notes_markdown_path).read_text(encoding="utf-8")

        self.assertEqual(item["text"], "watch NQ breakout after 10am")
        self.assertEqual(item["user_id"], 42)
        self.assertEqual(item["path"], str(Path(tmp) / "notes.md"))
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["source"], "note")
        self.assertIn("## 2026-05-20", text)
        self.assertIn("[note] watch NQ breakout after 10am", text)

    def test_append_note_redacts_secrets_and_caps_length(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = WatchdogConfig(
                notes_dir=str(Path(tmp) / "notes"),
                notes_markdown_path=str(Path(tmp) / "notes.md"),
                notes_max_chars=40,
            )
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
            cfg = WatchdogConfig(
                notes_dir=str(Path(tmp) / "notes"),
                notes_markdown_path=str(Path(tmp) / "notes.md"),
                notes_max_chars=2000,
            )
            eastern_evening = timezone(timedelta(hours=-4))
            store = NotesStore(
                cfg,
                now_provider=lambda: datetime(2026, 5, 19, 20, 30, tzinfo=eastern_evening),
            )

            store.append_note("after close note")
            text = Path(cfg.notes_markdown_path).read_text(encoding="utf-8")

            self.assertIn("## 2026-05-19", text)
            self.assertNotIn("## 2026-05-20", text)

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
        self.assertIn("2026-05-20 12:30Z", out)
        self.assertIn("review slippage", out)

    def test_extract_action_items_from_daily_report(self) -> None:
        out = extract_action_items(
            "\n".join(
                [
                    "\U0001F4B8 Transaction learnings",
                    "- one trade",
                    "",
                    "\u2705 Action items",
                    "- Tighten the VolBreakout stop.",
                    "2. Backtest a time filter.",
                    "",
                    "\U0001F4DD Notes",
                    "- ignore",
                ]
            )
        )

        self.assertEqual(out, ["Tighten the VolBreakout stop.", "Backtest a time filter."])

    def test_append_review_action_items_writes_notes_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = WatchdogConfig(
                notes_dir=str(Path(tmp) / "notes"),
                notes_markdown_path=str(Path(tmp) / "notes.md"),
                notes_max_chars=2000,
            )
            store = NotesStore(
                cfg,
                now_provider=lambda: datetime(2026, 5, 20, 12, 30, tzinfo=timezone.utc),
            )

            result = store.append_review_action_items(
                "\u2705 Action items\n- Review NQ stop width\n- Validate news filter",
                user_id=42,
                source="telegram /review",
            )
            text = (Path(tmp) / "notes.md").read_text(encoding="utf-8")

        self.assertEqual(result["items"], 2)
        self.assertIn("## 2026-05-20", text)
        self.assertIn("[review] Review NQ stop width", text)
        self.assertIn("[review] Validate news filter", text)

    def test_read_markdown_notes_reads_today_from_notes_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = WatchdogConfig(
                notes_dir=str(Path(tmp) / "notes"),
                notes_markdown_path=str(Path(tmp) / "notes.md"),
                notes_max_chars=2000,
            )
            notes_md = Path(cfg.notes_markdown_path)
            notes_md.write_text(
                "\n".join(
                    [
                        "## 2026-05-19 10:04 EDT",
                        "_Review action items (source=telegram /review)_",
                        "",
                        "- Old item",
                        "",
                        "## 2026-05-20",
                        "",
                        "- 12:30 [note] Review NQ stop width",
                        "- 12:31 [review] Validate news filter",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            store = NotesStore(
                cfg,
                now_provider=lambda: datetime(2026, 5, 20, 13, 0, tzinfo=timezone.utc),
            )

            items = store.read_markdown_notes(datetime(2026, 5, 20, tzinfo=timezone.utc).date())

        self.assertEqual([item["text"] for item in items], ["Review NQ stop width", "Validate news filter"])
        self.assertEqual(items[0]["time_label"], "12:30")
        self.assertEqual(items[0]["source"], "note")
        self.assertEqual(items[1]["source"], "review")

    def test_read_review_action_items_keeps_review_items_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = WatchdogConfig(
                notes_dir=str(Path(tmp) / "notes"),
                notes_markdown_path=str(Path(tmp) / "notes.md"),
                notes_max_chars=2000,
            )
            Path(cfg.notes_markdown_path).write_text(
                "\n".join(
                    [
                        "## 2026-05-20",
                        "",
                        "- 12:30 [note] manual note",
                        "- 12:31 [review] Review action",
                    ]
                ),
                encoding="utf-8",
            )
            store = NotesStore(
                cfg,
                now_provider=lambda: datetime(2026, 5, 20, 13, 0, tzinfo=timezone.utc),
            )

            items = store.read_review_action_items(datetime(2026, 5, 20, tzinfo=timezone.utc).date())

        self.assertEqual([item["text"] for item in items], ["Review action"])

    def test_format_notes_includes_review_action_item_time_label(self) -> None:
        out = format_notes(
            [
                {
                    "time_label": "12:30",
                    "text": "Review NQ stop width",
                }
            ],
            label="today",
        )

        self.assertIn("- 12:30 Review NQ stop width", out)

    def test_format_notes_includes_review_action_item_date_and_time_label(self) -> None:
        out = format_notes(
            [
                {
                    "date_label": "2026-05-20",
                    "time_label": "12:30",
                    "text": "Review NQ stop width",
                }
            ],
            label="last 7 days",
        )

        self.assertIn("- 2026-05-20 12:30 Review NQ stop width", out)

    def test_read_recent_notes_returns_last_7_days_from_notes_md_and_legacy_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = WatchdogConfig(
                notes_dir=str(Path(tmp) / "notes"),
                notes_markdown_path=str(Path(tmp) / "notes.md"),
                notes_max_chars=2000,
            )
            notes_dir = Path(cfg.notes_dir)
            notes_dir.mkdir(parents=True)
            (notes_dir / "2026-05-13.jsonl").write_text(
                '{"text": "old legacy note", "time_utc": "2026-05-13T12:00:00Z"}\n',
                encoding="utf-8",
            )
            (notes_dir / "2026-05-14.jsonl").write_text(
                '{"text": "legacy manual note", "time_utc": "2026-05-14T12:00:00Z"}\n',
                encoding="utf-8",
            )
            Path(cfg.notes_markdown_path).write_text(
                "\n".join(
                    [
                        "## 2026-05-20",
                        "",
                        "- 12:30 [note] Manual markdown note",
                        "- 12:31 [review] Review action",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            store = NotesStore(
                cfg,
                now_provider=lambda: datetime(2026, 5, 20, 13, 0, tzinfo=timezone.utc),
            )

            notes = store.read_recent_notes(days=7)

        self.assertEqual(
            [item["text"] for item in notes],
            ["legacy manual note", "Manual markdown note", "Review action"],
        )

    def test_load_config_resolves_notes_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.yaml"
            cfg_path.write_text(
                "\n".join(
                    [
                        "notes_enabled: false",
                        "notes_dir: runtime/notes",
                        "notes_markdown_path: runtime/notes.md",
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
        self.assertTrue(Path(cfg.notes_markdown_path).is_absolute())
        self.assertTrue(str(cfg.notes_dir).endswith(str(Path("runtime") / "notes")))
        self.assertTrue(str(cfg.notes_markdown_path).endswith(str(Path("runtime") / "notes.md")))


if __name__ == "__main__":
    unittest.main()
