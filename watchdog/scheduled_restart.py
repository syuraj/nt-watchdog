"""Scheduled NT restart via APScheduler.

Runs `recovery.manual_restart()` on a cron schedule (default Tue/Thu 17:30 ET),
then sends a /health snapshot to Telegram after a short delay so the operator
can confirm the restart landed cleanly.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .bridge_client import BridgeClient
from .config import WatchdogConfig
from .telegram_bot import (
    TelegramSharedState,
    _SharedSnapshot,
    format_health,
    snapshot_with_runtime,
)
from .telegram_notifier import TelegramNotifier


RestartHandler = Callable[[], Dict[str, Any]]


def parse_restart_time(value: str) -> Dict[str, int]:
    parts = str(value or "").strip().split(":")
    if len(parts) != 2:
        return {"hour": 17, "minute": 30}
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return {"hour": 17, "minute": 30}
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return {"hour": 17, "minute": 30}
    return {"hour": hour, "minute": minute}


def normalize_days(value: str) -> str:
    cleaned = ",".join(part.strip().lower() for part in str(value or "").split(",") if part.strip())
    return cleaned or "tue,thu"


class ScheduledRestartSender:
    def __init__(
        self,
        config: WatchdogConfig,
        notifier: TelegramNotifier,
        restart_handler: RestartHandler,
        shared_state: TelegramSharedState,
        bridge: BridgeClient,
    ) -> None:
        self.config = config
        self.notifier = notifier
        self.restart_handler = restart_handler
        self.shared_state = shared_state
        self.bridge = bridge
        self.scheduler = BackgroundScheduler()

    def start(self) -> None:
        if not self.config.scheduled_restart_enabled:
            return
        if not self.config.telegram_enabled:
            return
        if not self.config.telegram_bot_token or not self.config.telegram_chat_id:
            return

        hm = parse_restart_time(self.config.scheduled_restart_time)
        days = normalize_days(self.config.scheduled_restart_days)
        trigger = CronTrigger(
            day_of_week=days,
            hour=hm["hour"],
            minute=hm["minute"],
            timezone=self.config.scheduled_restart_timezone,
        )
        self.scheduler.add_job(self._run_restart, trigger)
        self.scheduler.start()

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def _run_restart(self) -> None:
        self.notifier.send_message(
            "\U0001F501 Scheduled restart starting (forceful, may take ~2 min)..."
        )
        try:
            result = self.restart_handler() or {}
        except Exception as exc:  # pragma: no cover - defensive
            self.notifier.send_message(f"\u274c Scheduled restart failed: {exc!r}")
            return

        if not result.get("ok"):
            err = str(result.get("error") or "unknown_error")
            self.notifier.send_message(f"\u274c Scheduled restart failed: {err}")
            return

        toggled = int(result.get("strategies_toggled", 0) or 0)
        self.notifier.send_message(
            f"\u2705 Scheduled restart complete. Strategies re-enabled: {toggled}. "
            f"Health check in {self.config.scheduled_restart_health_delay_sec}s."
        )

        delay = max(0, int(self.config.scheduled_restart_health_delay_sec))
        timer = threading.Timer(delay, self._send_health_report)
        timer.daemon = True
        timer.start()

    def _send_health_report(self) -> None:
        try:
            health = self.bridge.safe_health()
            runtime_snapshot = self.bridge.safe_runtime_snapshot()
        except Exception as exc:  # pragma: no cover - defensive
            self.notifier.send_message(f"\u26a0\ufe0f Post-restart health fetch failed: {exc!r}")
            return

        # Push fresh data into shared state so format_health renders the post-restart view.
        try:
            self.shared_state.publish(health, runtime_snapshot)
        except Exception:
            pass

        snap = _SharedSnapshot(health=health, runtime_snapshot=runtime_snapshot)
        snap = snapshot_with_runtime(snap, runtime_snapshot)
        message = "\U0001F3E5 Post-restart health\n" + format_health(snap)
        self.notifier.send_message(message)
