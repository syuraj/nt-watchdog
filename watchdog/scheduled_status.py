"""Scheduled status updates via APScheduler.

Schedules status sends at 12:30 PM and 4:00 PM on weekdays.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, List

from apscheduler.schedulers.background import BackgroundScheduler

from .config import WatchdogConfig
from .telegram_bot import (
    _SharedSnapshot,
    format_status,
    load_sqlite_daily_activity,
    merge_daily_activity,
)
from .telegram_notifier import TelegramNotifier


class ScheduledStatusSender:
    """Manages scheduled status sends using APScheduler."""

    def __init__(
        self,
        config: WatchdogConfig,
        notifier: TelegramNotifier,
        snapshot_provider: Callable[[], _SharedSnapshot],
        bridge_snapshot_provider: Callable[[], Dict[str, Any]],
        bridge_positions_provider: Callable[[], List[Dict[str, Any]]],
    ):
        self.config = config
        self.notifier = notifier
        self.snapshot_provider = snapshot_provider
        self.bridge_snapshot_provider = bridge_snapshot_provider
        self.bridge_positions_provider = bridge_positions_provider
        self.scheduler = BackgroundScheduler()

    def start(self) -> None:
        """Start scheduled status sender."""
        if not self.config.telegram_enabled:
            return

        if not self.config.telegram_bot_token or not self.config.telegram_chat_id:
            return

        # Schedule 12:30 PM weekdays
        self.scheduler.add_job(
            self._send_status,
            "cron",
            day_of_week="mon-fri",
            hour=12,
            minute=30,
            args=["12:30 PM"],
        )

        # Schedule 4:00 PM weekdays
        self.scheduler.add_job(
            self._send_status,
            "cron",
            day_of_week="mon-fri",
            hour=16,
            minute=0,
            args=["4:00 PM"],
        )

        self.scheduler.start()

    def stop(self) -> None:
        """Stop scheduled status sender."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def _send_status(self, time_label: str) -> None:
        """Send scheduled status update."""
        now = datetime.now()

        # Fetch fresh data
        try:
            runtime_snapshot = self.bridge_snapshot_provider()
            positions = self.bridge_positions_provider()
            sqlite_activity = load_sqlite_daily_activity()
        except Exception:
            # Bridge down, send fallback
            fallback = f"⚠️ Scheduled status unavailable - bridge error at {time_label}"
            self.notifier.send_message(fallback)
            return

        if runtime_snapshot.get("error"):
            fallback = f"⚠️ Scheduled status unavailable - bridge down at {time_label}"
            self.notifier.send_message(fallback)
            return

        # Format status
        snap = _SharedSnapshot(
            health=runtime_snapshot.get("health", {}),
            runtime_snapshot=runtime_snapshot,
            last_cycle_utc=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            started_utc="",
        )

        merged_activity = merge_daily_activity(sqlite_activity)
        status_text = format_status(snap, merged_activity, positions_override=positions)

        # Prepend scheduled header
        header = f"📊 Scheduled Update ({time_label})\n\n"
        message = header + status_text

        # Send
        self.notifier.send_message(message)
