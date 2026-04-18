from __future__ import annotations

import unittest

from watchdog.config import WatchdogConfig
from watchdog.telegram_notifier import TelegramNotifier


class TelegramNotifierTests(unittest.TestCase):
    def test_disabled_when_no_secrets(self) -> None:
        cfg = WatchdogConfig(telegram_enabled=True, telegram_bot_token="", telegram_chat_id="")
        notifier = TelegramNotifier(cfg)
        self.assertFalse(notifier.enabled)

    def test_disabled_when_config_off(self) -> None:
        cfg = WatchdogConfig(
            telegram_enabled=False,
            telegram_bot_token="token",
            telegram_chat_id="chat",
        )
        notifier = TelegramNotifier(cfg)
        self.assertFalse(notifier.enabled)


if __name__ == "__main__":
    unittest.main()

