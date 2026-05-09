from __future__ import annotations

import threading
import unittest
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from watchdog.config import WatchdogConfig
from watchdog.telegram_bot import (
    TelegramBotService,
    TelegramSharedState,
    format_commands,
    format_health,
    format_status,
    load_sqlite_daily_activity,
    merge_daily_activity,
    snapshot_with_runtime,
)


class FormatHealthTests(unittest.TestCase):
    def test_connected_with_active_strategies(self) -> None:
        state = TelegramSharedState()
        state.publish(
            {"status": "ok", "connections": {"total": 1, "connected": 1}},
            {
                "strategy_runtime": {
                    "strategies": [
                        {"name": "S1", "is_enabled": True, "state": "Realtime"},
                        {"name": "S2", "is_enabled": True, "state": "Realtime"},
                    ]
                }
            },
        )
        out = format_health(state.snapshot())
        self.assertIn("🟢", out)
        self.assertIn("NT connections: 1/1", out)
        self.assertIn("Strategies: 2 active / 2 total", out)
        self.assertIn("S1 (active)", out)
        self.assertIn("S2 (active)", out)
        self.assertIn("Health: ok", out)

    def test_disconnected_shows_red(self) -> None:
        state = TelegramSharedState()
        state.publish(
            {"status": "degraded", "connections": {"total": 1, "connected": 0}, "reasons": ["no_connections_detected"]},
            {"strategy_runtime": {"strategies": []}},
        )
        out = format_health(state.snapshot())
        self.assertIn("🔴", out)
        self.assertIn("Strategies: 0 active / 0 total", out)
        self.assertIn("Reasons: no_connections_detected", out)

    def test_disabled_strategy_marked_off(self) -> None:
        state = TelegramSharedState()
        state.publish(
            {"status": "ok", "connections": {"total": 1, "connected": 1}},
            {
                "strategy_runtime": {
                    "strategies": [
                        {"name": "S1", "is_enabled": False, "state": "Finalized"},
                    ]
                }
            },
        )
        out = format_health(state.snapshot())
        self.assertIn("S1 (off/Finalized)", out)

    def test_missing_fields_render_safely(self) -> None:
        state = TelegramSharedState()
        # No publish call — snapshot is empty dicts.
        out = format_health(state.snapshot())
        self.assertIn("Health: unknown", out)
        self.assertIn("NT connections: ?/?", out)


class FormatStatusTests(unittest.TestCase):
    def _publish(self, accounts, positions) -> TelegramSharedState:
        state = TelegramSharedState()
        state.publish(
            {"status": "ok"},
            {"accounts": accounts, "positions": positions, "strategy_runtime": {"strategies": []}},
        )
        return state

    def test_connected_account_with_position_and_pnl(self) -> None:
        state = self._publish(
            accounts=[
                {"name": "DEMO", "connected": True, "cash": 49269.24,
                 "realized_pnl": 120.0, "unrealized_pnl": -5.5, "buying_power": 0.0},
            ],
            positions=[
                {"account": "DEMO", "instrument": "ES 06-26", "side": "Long",
                 "quantity": 2, "avg_price": 5000.25, "unrealized": -5.5},
            ],
        )
        daily = [{"account": "DEMO", "total_pnl": 114.5, "trades": 3, "wins": 2, "losses": 1}]
        out = format_status(state.snapshot(), daily)
        self.assertIn("DEMO", out)
        self.assertIn("$49,269.24", out)
        self.assertIn("Today:", out)
        self.assertIn("$114.50", out)
        self.assertIn("3 closed (2W/1L)", out)
        self.assertIn("ES 06-26 Long 2 @ $5,000.25", out)
        # Realized/Unrealized account-level line removed.
        self.assertNotIn("Realized:", out)
        self.assertNotIn("Unrealized:", out)
        # No separate Positions section.
        self.assertNotIn("📊", out)

    def test_open_position_without_closed_trade_shows_account(self) -> None:
        state = self._publish(
            accounts=[
                {"name": "Live", "connected": True, "cash": 25000.0,
                 "realized_pnl": 0.0, "unrealized_pnl": 10.0},
            ],
            positions=[
                {"account": "Live", "instrument": "NQ 06-26", "side": "Long",
                 "quantity": 1, "avg_price": 20000.0, "unrealized": 10.0},
            ],
        )
        # trades=0 but a position is open → account must still show.
        out = format_status(state.snapshot(), [])
        self.assertIn("Live", out)
        self.assertIn("NQ 06-26 Long 1", out)
        self.assertIn("0 closed", out)

    def test_positions_override_replaces_cached(self) -> None:
        state = self._publish(
            accounts=[{"name": "A", "connected": True, "cash": 100.0,
                       "realized_pnl": 0.0, "unrealized_pnl": 0.0}],
            positions=[
                {"account": "A", "instrument": "ES 06-26", "side": "Long",
                 "quantity": 1, "avg_price": 5000, "unrealized": 10},
            ],
        )
        # Live fetch returns empty → stale ES line must NOT appear.
        out = format_status(state.snapshot(), [], positions_override=[])
        self.assertNotIn("ES 06-26", out)
        self.assertIn("No accounts traded or holding positions today", out)

    def test_positions_override_none_falls_back_to_snapshot(self) -> None:
        state = self._publish(
            accounts=[{"name": "A", "connected": True, "cash": 100.0,
                       "realized_pnl": 0.0, "unrealized_pnl": 0.0}],
            positions=[
                {"account": "A", "instrument": "ES 06-26", "side": "Long",
                 "quantity": 1, "avg_price": 5000, "unrealized": 10},
            ],
        )
        out = format_status(state.snapshot(), [], positions_override=None)
        self.assertIn("ES 06-26", out)

    def test_duplicate_pnl_rows_last_wins(self) -> None:
        state = self._publish(
            accounts=[{"name": "A", "connected": True, "cash": 100.0,
                       "realized_pnl": 0.0, "unrealized_pnl": 0.0}],
            positions=[],
        )
        daily = [
            {"account": "A", "total_pnl": 10.0, "trades": 1, "wins": 1, "losses": 0},
            {"account": "A", "total_pnl": 50.0, "trades": 5, "wins": 3, "losses": 2},
        ]
        out = format_status(state.snapshot(), daily)
        self.assertIn("$50.00", out)
        self.assertIn("5 closed", out)
        self.assertNotIn("$10.00", out)

    def test_connected_non_traded_account_filtered_out(self) -> None:
        state = self._publish(
            accounts=[
                {"name": "Idle", "connected": True, "cash": 50000.0,
                 "realized_pnl": 0.0, "unrealized_pnl": 0.0},
                {"name": "Active", "connected": True, "cash": 10000.0,
                 "realized_pnl": 50.0, "unrealized_pnl": 0.0},
            ],
            positions=[],
        )
        daily = [
            {"account": "Idle", "total_pnl": 0.0, "trades": 0, "wins": 0, "losses": 0},
            {"account": "Active", "total_pnl": 50.0, "trades": 2, "wins": 2, "losses": 0},
        ]
        out = format_status(state.snapshot(), daily)
        self.assertNotIn("Idle", out)
        self.assertIn("Active", out)

    def test_zero_closed_trades_with_pnl_still_shows_account(self) -> None:
        state = self._publish(
            accounts=[
                {"name": "Active", "connected": True, "cash": 10000.0,
                 "realized_pnl": 0.0, "unrealized_pnl": 0.0},
            ],
            positions=[],
        )
        daily = [
            {"account": "Active", "total_pnl": 125.5, "realized_pnl": 125.5,
             "unrealized_pnl": 0.0, "trades": 0, "wins": 0, "losses": 0},
        ]
        out = format_status(state.snapshot(), daily)
        self.assertIn("Active", out)
        self.assertIn("$125.50", out)
        self.assertIn("0 closed", out)

    def test_zero_closed_trades_with_executions_still_shows_account(self) -> None:
        state = self._publish(
            accounts=[
                {"name": "Active", "connected": True, "cash": 10000.0,
                 "realized_pnl": 0.0, "unrealized_pnl": 0.0},
            ],
            positions=[],
        )
        daily = [
            {"account": "Active", "total_pnl": 0.0, "trades": 0,
             "executions": 4, "wins": 0, "losses": 0},
        ]
        out = format_status(state.snapshot(), daily)
        self.assertIn("Active", out)
        self.assertIn("0 closed", out)
        self.assertIn("4 executions", out)

    def test_has_activity_today_still_shows_account(self) -> None:
        state = self._publish(
            accounts=[
                {"name": "Scratch", "connected": True, "cash": 10000.0,
                 "realized_pnl": 0.0, "unrealized_pnl": 0.0},
            ],
            positions=[],
        )
        daily = [
            {"account": "Scratch", "total_pnl": 0.0, "trades": 0,
             "executions": 0, "wins": 0, "losses": 0, "has_activity_today": True},
        ]
        out = format_status(state.snapshot(), daily)
        self.assertIn("Scratch", out)
        self.assertIn("0 closed", out)

    def test_disconnected_account_filtered_out(self) -> None:
        state = self._publish(
            accounts=[
                {"name": "StaleButTraded", "connected": False, "cash": 100.0,
                 "realized_pnl": 10.0, "unrealized_pnl": 0.0},
            ],
            positions=[],
        )
        daily = [{"account": "StaleButTraded", "total_pnl": 10.0, "trades": 1, "wins": 1, "losses": 0}]
        out = format_status(state.snapshot(), daily)
        self.assertNotIn("StaleButTraded", out)
        self.assertIn("No accounts traded or holding positions today", out)

    def test_no_trades_fallback(self) -> None:
        state = self._publish(
            accounts=[{"name": "Sim101", "connected": True, "cash": 1000.0,
                       "realized_pnl": 0.0, "unrealized_pnl": 0.0}],
            positions=[],
        )
        out = format_status(state.snapshot(), [])
        self.assertIn("No accounts traded or holding positions today", out)

    def test_empty_snapshot_fallback(self) -> None:
        state = TelegramSharedState()
        out = format_status(state.snapshot(), [])
        self.assertIn("No snapshot yet", out)

    def test_fresh_runtime_snapshot_replaces_cached_accounts(self) -> None:
        state = self._publish(
            accounts=[{"name": "Stale", "connected": True, "cash": 100.0}],
            positions=[],
        )
        fresh = snapshot_with_runtime(
            state.snapshot(),
            {
                "health": {"status": "ok"},
                "accounts": [{"name": "Fresh", "connected": True, "cash": 200.0}],
                "positions": [],
            },
        )
        daily = [{"account": "Fresh", "total_pnl": 25.0, "trades": 1, "wins": 1, "losses": 0}]
        out = format_status(fresh, daily, positions_override=[])
        self.assertIn("Fresh", out)
        self.assertNotIn("Stale", out)

    def test_position_for_disconnected_account_hidden(self) -> None:
        state = self._publish(
            accounts=[{"name": "Stale", "connected": False, "cash": 1000.0,
                       "realized_pnl": 0.0, "unrealized_pnl": 0.0}],
            positions=[
                {"account": "Stale", "instrument": "NQ", "side": "Short",
                 "quantity": 1, "avg_price": 20000, "unrealized": 0},
            ],
        )
        out = format_status(state.snapshot(), [])
        self.assertNotIn("NQ", out)
        self.assertNotIn("Stale", out)


class DailyActivityFallbackTests(unittest.TestCase):
    def _ticks(self, value: datetime) -> int:
        epoch = datetime(1, 1, 1, tzinfo=timezone.utc)
        return int((value.astimezone(timezone.utc) - epoch).total_seconds() * 10000000)

    def test_merge_sqlite_activity_marks_zero_bridge_row_active(self) -> None:
        daily = [{"account": "SimA", "total_pnl": 0.0, "trades": 0, "executions": 0}]
        sqlite_rows = [{"account": "SimA", "executions": 2, "has_activity_today": True}]

        merged = merge_daily_activity(daily, sqlite_rows)

        self.assertEqual(merged[0]["account"], "SimA")
        self.assertEqual(merged[0]["executions"], 2)
        self.assertTrue(merged[0]["has_activity_today"])
        self.assertEqual(merged[0]["activity_source"], "sqlite")

    def test_sqlite_activity_loader_counts_local_day_executions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "NinjaTrader.sqlite"
            con = sqlite3.connect(db_path)
            try:
                con.execute("create table Accounts (Id integer primary key, Name text)")
                con.execute("create table Executions (Account integer, Time integer)")
                con.execute("insert into Accounts values (1, 'SimA')")
                con.execute("insert into Accounts values (2, 'Old')")
                now = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc).astimezone()
                con.execute("insert into Executions values (1, ?)", (self._ticks(now),))
                con.execute("insert into Executions values (1, ?)", (self._ticks(now.replace(hour=13)),))
                con.execute("insert into Executions values (2, ?)", (self._ticks(now.replace(day=7)),))
                con.commit()
            finally:
                con.close()

            rows = load_sqlite_daily_activity(db_path, now=now)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["account"], "SimA")
        self.assertEqual(rows[0]["executions"], 2)
        self.assertTrue(rows[0]["has_activity_today"])


class FormatCommandsTests(unittest.TestCase):
    def test_contains_all_commands(self) -> None:
        out = format_commands()
        self.assertIn("/health", out)
        self.assertIn("/status", out)
        self.assertIn("/restart", out)
        self.assertNotIn("/help", out)


class FormatRestartResultTests(unittest.TestCase):
    def test_success(self) -> None:
        from watchdog.telegram_bot import format_restart_result

        out = format_restart_result(
            {"ok": True, "strategies_toggled": 3, "bridge_up": True}
        )
        self.assertIn("✅", out)
        self.assertIn("Strategies enabled: 3", out)
        self.assertNotIn("bridge not yet responsive", out)

    def test_success_bridge_not_up_note(self) -> None:
        from watchdog.telegram_bot import format_restart_result

        out = format_restart_result(
            {"ok": True, "strategies_toggled": 0, "bridge_up": False}
        )
        self.assertIn("bridge not yet responsive", out)

    def test_failure(self) -> None:
        from watchdog.telegram_bot import format_restart_result

        out = format_restart_result({"ok": False, "error": "process_restart_failed"})
        self.assertIn("❌", out)
        self.assertIn("process_restart_failed", out)


class SharedStateThreadSafetyTests(unittest.TestCase):
    def test_concurrent_publish_and_read(self) -> None:
        state = TelegramSharedState()
        errors: list = []

        def writer() -> None:
            for i in range(200):
                state.publish(
                    {"status": "ok", "connections": {"total": 1, "connected": 1}, "_i": i},
                    {"strategy_runtime": {"strategies": []}},
                )

        def reader() -> None:
            for _ in range(200):
                try:
                    snap = state.snapshot()
                    # Assert dict integrity — no torn reads
                    self.assertIsInstance(snap.health, dict)
                    self.assertIsInstance(snap.runtime_snapshot, dict)
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=writer) for _ in range(3)] + [
            threading.Thread(target=reader) for _ in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


class TelegramBotServiceEnabledTests(unittest.TestCase):
    def _config(self, **overrides) -> WatchdogConfig:
        base = WatchdogConfig(
            telegram_enabled=True,
            telegram_bot_token="123:abc",
            telegram_allowed_user_ids=[42],
        )
        for k, v in overrides.items():
            setattr(base, k, v)
        return base

    def test_enabled_when_configured(self) -> None:
        svc = TelegramBotService(self._config(), TelegramSharedState())
        self.assertTrue(svc.enabled)

    def test_disabled_without_token(self) -> None:
        svc = TelegramBotService(self._config(telegram_bot_token=""), TelegramSharedState())
        self.assertFalse(svc.enabled)

    def test_disabled_without_allowed_users(self) -> None:
        svc = TelegramBotService(self._config(telegram_allowed_user_ids=[]), TelegramSharedState())
        self.assertFalse(svc.enabled)

    def test_disabled_when_notifier_off(self) -> None:
        svc = TelegramBotService(self._config(telegram_enabled=False), TelegramSharedState())
        self.assertFalse(svc.enabled)

    def test_start_is_noop_when_disabled(self) -> None:
        svc = TelegramBotService(self._config(telegram_bot_token=""), TelegramSharedState())
        self.assertFalse(svc.start())
        self.assertIn("disabled", svc.last_error)
        # Safe to stop even when never started.
        svc.stop(timeout_sec=0.1)


if __name__ == "__main__":
    unittest.main()
