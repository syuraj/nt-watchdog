from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from watchdog.config import WatchdogConfig, load_config
from watchdog.scheduled_daily_report import (
    ScheduledDailyReportSender,
    build_daily_report_question,
    build_daily_report_prompt,
    format_daily_report_message,
    is_market_session_day,
    load_daily_transactions,
    parse_report_time,
)
from watchdog.telegram_bot import _dotnet_ticks


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_message(self, text: str) -> bool:
        self.messages.append(text)
        return True


class DailyReportHelpersTests(unittest.TestCase):
    def test_parse_report_time_defaults_invalid_values_to_7pm(self) -> None:
        self.assertEqual(parse_report_time("19:00"), (19, 0))
        self.assertEqual(parse_report_time("7pm"), (19, 0))
        self.assertEqual(parse_report_time("25:10"), (19, 0))

    def test_weekend_is_not_market_session_day(self) -> None:
        should_run, reason = is_market_session_day(datetime(2026, 5, 16).date())
        self.assertFalse(should_run)
        self.assertEqual(reason, "weekend")

    def test_prompt_requests_transactions_errors_and_strategy_improvements(self) -> None:
        prompt = build_daily_report_prompt("2026-05-19")
        self.assertIn("\U0001F4B8 Transaction learnings", prompt)
        self.assertIn("\U0001F6E0\ufe0f Strategy improvement ideas", prompt)
        self.assertIn("\u26a0\ufe0f NT/watchdog issues", prompt)
        self.assertIn("\u2705 Action items", prompt)
        self.assertIn("Transaction learnings", prompt)
        self.assertIn("Strategy improvement ideas", prompt)
        self.assertIn("NT/watchdog issues", prompt)
        self.assertIn("short bullets", prompt)
        self.assertIn("Do not wrap", prompt)

    def test_format_daily_report_message_adds_emoji_header_and_footer(self) -> None:
        out = format_daily_report_message("body", 3500, footer="\U0001F4DD saved")
        self.assertEqual(out, "\U0001F4CA Daily learning report\n\nbody\n\n\U0001F4DD saved")

    def test_format_daily_report_message_strips_markdown_backticks(self) -> None:
        out = format_daily_report_message("Review `NQ` and ```logs```", 3500)
        self.assertEqual(out, "\U0001F4CA Daily learning report\n\nReview NQ and logs")


class DailyTransactionsTests(unittest.TestCase):
    def test_load_daily_transactions_reads_today_executions_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "NinjaTrader.sqlite"
            import sqlite3

            con = sqlite3.connect(db_path)
            try:
                con.executescript(
                    """
                    create table Accounts (Id integer primary key, Name text);
                    create table MasterInstruments (Id integer primary key, Name text);
                    create table Instruments (Id integer primary key, FullName text, MasterInstrument integer);
                    create table Orders (Id integer primary key, Name text, OrderAction integer, OrderState integer);
                    create table Executions (
                        Id integer primary key,
                        Account integer,
                        Instrument integer,
                        [Order] integer,
                        Time integer,
                        MarketPosition integer,
                        Price real,
                        Quantity integer,
                        Commission real,
                        Fee real
                    );
                    """
                )
                con.execute("insert into Accounts values (1, 'Sim')")
                con.execute("insert into MasterInstruments values (1, 'NQ')")
                con.execute("insert into Instruments values (1, 'NQ 06-26', 1)")
                con.execute("insert into Orders values (1, 'GapOrbEntry', 0, 2)")
                now = datetime(2026, 5, 19, 19, 0, tzinfo=timezone.utc)
                con.execute(
                    "insert into Executions values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (1, 1, 1, 1, _dotnet_ticks(now), 0, 21000.25, 1, 2.5, 0.0),
                )
                con.commit()
            finally:
                con.close()

            out = load_daily_transactions(db_path, now=now)

        self.assertEqual(out["count"], 1)
        self.assertEqual(out["by_account"]["Sim"]["executions"], 1)
        self.assertEqual(out["by_instrument"]["NQ 06-26"]["quantity"], 1)
        self.assertEqual(out["executions"][0]["order_name"], "GapOrbEntry")

    def test_load_daily_transactions_uses_master_name_and_order_id_join(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "NinjaTrader.sqlite"
            import sqlite3

            con = sqlite3.connect(db_path)
            try:
                con.executescript(
                    """
                    create table Accounts (Id integer primary key, Name text);
                    create table MasterInstruments (Id integer primary key, Name text);
                    create table Instruments (Id integer primary key, MasterInstrument integer);
                    create table Orders (OrderId text primary key, Name text, OrderAction integer, OrderState integer);
                    create table Executions (
                        Id integer primary key,
                        Account integer,
                        Instrument integer,
                        OrderId text,
                        Time integer,
                        MarketPosition integer,
                        Price real,
                        Quantity integer,
                        Commission real,
                        Fee real
                    );
                    """
                )
                con.execute("insert into Accounts values (1, 'Sim')")
                con.execute("insert into MasterInstruments values (7, 'NQ')")
                con.execute("insert into Instruments values (42, 7)")
                con.execute("insert into Orders values ('ord-1', 'NqHitterEntry', 0, 2)")
                now = datetime(2026, 5, 19, 19, 0, tzinfo=timezone.utc)
                con.execute(
                    "insert into Executions values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (1, 1, 42, "ord-1", _dotnet_ticks(now), 0, 21000.25, 1, 2.5, 0.0),
                )
                con.commit()
            finally:
                con.close()

            out = load_daily_transactions(db_path, now=now)

        self.assertEqual(out["count"], 1)
        self.assertIn("NQ", out["by_instrument"])
        self.assertNotIn("42", out["by_instrument"])
        self.assertEqual(out["executions"][0]["instrument"], "NQ")
        self.assertEqual(out["executions"][0]["instrument_id"], 42)
        self.assertEqual(out["executions"][0]["order_name"], "NqHitterEntry")
        self.assertEqual(out["executions"][0]["order_id"], "ord-1")


class ScheduledDailyReportSenderTests(unittest.TestCase):
    def test_send_report_skips_closed_market_day(self) -> None:
        notifier = FakeNotifier()
        cfg = WatchdogConfig(telegram_enabled=True, telegram_bot_token="t", telegram_chat_id="c")
        sender = ScheduledDailyReportSender(
            cfg,
            notifier,  # type: ignore[arg-type]
            codex_runner=lambda _cfg, _question: "should not run",
            now_provider=lambda: datetime(2026, 5, 16, 19, 0, tzinfo=timezone.utc),
        )

        sender._send_report()

        self.assertEqual(notifier.messages, [])

    def test_send_report_sends_codex_answer_with_daily_context(self) -> None:
        notifier = FakeNotifier()
        cfg = WatchdogConfig(
            telegram_enabled=True,
            telegram_bot_token="t",
            telegram_chat_id="c",
            daily_report_market_calendar="XNYS",
        )
        calls: list[str] = []

        def runner(_cfg, question: str) -> str:
            calls.append(question)
            return "report body"

        sender = ScheduledDailyReportSender(
            cfg,
            notifier,  # type: ignore[arg-type]
            codex_runner=runner,
            now_provider=lambda: datetime(2026, 5, 19, 19, 0, tzinfo=timezone.utc),
        )

        with mock.patch(
            "watchdog.scheduled_daily_report.is_market_session_day",
            return_value=(True, "market_open:test"),
        ), mock.patch(
            "watchdog.scheduled_daily_report.build_daily_report_context",
            return_value="prefetched context",
        ):
            sender._send_report()

        self.assertEqual(notifier.messages, ["\U0001F4CA Daily learning report\n\nreport body"])
        self.assertEqual(len(calls), 1)
        self.assertIn("prefetched context", calls[0])
        self.assertIn("Market-day gate: market_open:test", calls[0])

    def test_manual_review_question_builds_daily_context_without_market_skip(self) -> None:
        cfg = WatchdogConfig()
        with mock.patch(
            "watchdog.scheduled_daily_report.build_daily_report_context",
            return_value="prefetched context",
        ):
            out = build_daily_report_question(
                cfg,
                datetime(2026, 5, 19, 19, 0, tzinfo=timezone.utc),
                market_reason="manual_telegram",
            )

        self.assertIn("Daily report: analyze 2026-05-19", out)
        self.assertIn("prefetched context", out)
        self.assertIn("Market-day gate: manual_telegram", out)


class DailyReportConfigTests(unittest.TestCase):
    def test_load_config_reads_daily_report_settings_and_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.yaml"
            cfg_path.write_text(
                "\n".join(
                    [
                        "daily_report_enabled: false",
                        'daily_report_time: "18:30"',
                        'daily_report_market_calendar: "CME_Equity"',
                        "daily_report_require_market_calendar: true",
                    ]
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                "os.environ",
                {
                    "DAILY_REPORT_ENABLED": "true",
                    "DAILY_REPORT_TIME": "19:15",
                    "DAILY_REPORT_MAX_REPLY_CHARS": "1200",
                },
                clear=False,
            ):
                cfg = load_config(str(cfg_path))

        self.assertTrue(cfg.daily_report_enabled)
        self.assertEqual(cfg.daily_report_time, "19:15")
        self.assertEqual(cfg.daily_report_market_calendar, "CME_Equity")
        self.assertTrue(cfg.daily_report_require_market_calendar)
        self.assertEqual(cfg.daily_report_max_reply_chars, 1200)


if __name__ == "__main__":
    unittest.main()
