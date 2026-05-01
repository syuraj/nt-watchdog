from __future__ import annotations

import threading
import unittest

from watchdog.config import WatchdogConfig
from watchdog.telegram_bot import (
    TelegramBotService,
    TelegramSharedState,
    format_commands,
    format_health,
    format_status,
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
        self.assertIn("3 trades (2W/1L)", out)
        self.assertIn("ES 06-26 Long 2 @ $5,000.25", out)

    def test_disconnected_account_filtered_out(self) -> None:
        state = self._publish(
            accounts=[
                {"name": "Backtest", "connected": False, "cash": 100000.0,
                 "realized_pnl": 0.0, "unrealized_pnl": 0.0},
                {"name": "Sim101", "connected": True, "cash": 102545.0,
                 "realized_pnl": 0.0, "unrealized_pnl": 0.0},
            ],
            positions=[],
        )
        out = format_status(state.snapshot(), [])
        self.assertNotIn("Backtest", out)
        self.assertIn("Sim101", out)
        self.assertIn("Positions: none", out)

    def test_no_pnl_row_skips_today_line(self) -> None:
        state = self._publish(
            accounts=[{"name": "Sim101", "connected": True, "cash": 1000.0,
                       "realized_pnl": 0.0, "unrealized_pnl": 0.0}],
            positions=[],
        )
        out = format_status(state.snapshot(), [])
        self.assertNotIn("Today:", out)

    def test_empty_snapshot_fallback(self) -> None:
        state = TelegramSharedState()
        out = format_status(state.snapshot(), [])
        self.assertIn("No snapshot yet", out)

    def test_position_for_disconnected_account_hidden(self) -> None:
        state = self._publish(
            accounts=[{"name": "Sim101", "connected": True, "cash": 1000.0,
                       "realized_pnl": 0.0, "unrealized_pnl": 0.0}],
            positions=[
                {"account": "Stale", "instrument": "NQ", "side": "Short",
                 "quantity": 1, "avg_price": 20000, "unrealized": 0},
            ],
        )
        out = format_status(state.snapshot(), [])
        self.assertNotIn("NQ", out)
        self.assertIn("Positions: none", out)


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
