from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict

from .config import WatchdogConfig


class TelegramNotifier:
    def __init__(self, config: WatchdogConfig) -> None:
        self.config = config
        self.last_error = ""

    @property
    def enabled(self) -> bool:
        return bool(
            self.config.telegram_enabled
            and self.config.telegram_bot_token
            and self.config.telegram_chat_id
        )

    def _post_message(self, text: str, parse_mode: str = "") -> bool:
        base = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": self.config.telegram_chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(base, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        ok = bool(body.get("ok"))
        if not ok:
            self.last_error = f"telegram api returned ok=false: {body}"
        return ok

    @staticmethod
    def _format_exception(exc: Exception) -> str:
        if isinstance(exc, urllib.error.HTTPError):
            try:
                payload = exc.read().decode("utf-8", errors="replace")
            except Exception:
                payload = "<unreadable>"
            return f"HTTP {exc.code}: {payload}"
        return f"{type(exc).__name__}: {exc}"

    def send_message(self, text: str) -> bool:
        self.last_error = ""
        if not self.enabled:
            self.last_error = "notifier disabled or missing bot token/chat id"
            return False
        first_error = ""
        try:
            if self._post_message(text, self.config.telegram_parse_mode):
                return True
            first_error = self.last_error or "parse_mode send returned ok=false"
        except Exception as exc:
            first_error = self._format_exception(exc)
        # Fallback to plain text if parse_mode attempt fails.
        try:
            if self._post_message(text, ""):
                return True
            second_error = self.last_error or "plain-text fallback returned ok=false"
            self.last_error = f"primary send failed ({first_error}); fallback failed ({second_error})"
            return False
        except Exception as exc2:
            self.last_error = (
                f"primary send failed ({first_error}); fallback exception ({self._format_exception(exc2)})"
            )
            return False

    def notify_event(self, event_type: str, incident_id: str, details: Dict[str, Any]) -> bool:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        status = details.get("status", "unknown")
        action = details.get("action", "none")
        reason = details.get("reason", "")
        message = (
            f"*NT8 Watchdog* `{event_type}`\n"
            f"- incident: `{incident_id}`\n"
            f"- time_utc: `{ts}`\n"
            f"- status: `{status}`\n"
            f"- action: `{action}`\n"
            f"- reason: `{reason}`"
        )
        return self.send_message(message)

