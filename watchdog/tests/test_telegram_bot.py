from __future__ import annotations

import threading
import unittest

from watchdog.config import WatchdogConfig
from watchdog.telegram_bot import (
    TelegramBotService,
    TelegramSharedState,
    format_help,
    format_status,
)


class FormatStatusTests(unittest.TestCase):
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
        out = format_status(state.snapshot())
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
        out = format_status(state.snapshot())
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
        out = format_status(state.snapshot())
        self.assertIn("S1 (off/Finalized)", out)

    def test_missing_fields_render_safely(self) -> None:
        state = TelegramSharedState()
        # No publish call — snapshot is empty dicts.
        out = format_status(state.snapshot())
        self.assertIn("Health: unknown", out)
        self.assertIn("NT connections: ?/?", out)


class FormatHelpTests(unittest.TestCase):
    def test_contains_both_commands(self) -> None:
        out = format_help()
        self.assertIn("/status", out)
        self.assertIn("/help", out)


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
