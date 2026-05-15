"""Scheduled status updates via threading.

Runs on watchdog main thread, schedules status sends at configured times.
"""

from __future__ import annotations

import threading
from datetime import datetime, time as dt_time, timedelta
from typing import Any, Callable, Dict, List, Optional

from .config import WatchdogConfig
from .telegram_bot import (
    _SharedSnapshot,
    format_status,
    load_sqlite_daily_activity,
    merge_daily_activity,
)
from .telegram_notifier import TelegramNotifier


def _next_scheduled_time(schedule_times: List[dt_time]) -> Optional[datetime]:
    """Calculate next scheduled time from now."""
    if not schedule_times:
        return None

    now = datetime.now()
    today = now.date()

    # Check if any scheduled time today is still in future
    for sched_time in sorted(schedule_times):
        next_run = datetime.combine(today, sched_time)
        if next_run > now:
            # Only weekdays
            if next_run.weekday() < 5:
                return next_run

    # All today's times passed, find next weekday occurrence
    tomorrow = today + timedelta(days=1)
    for day_offset in range(1, 8):  # Check next 7 days
        candidate = today + timedelta(days=day_offset)
        if candidate.weekday() < 5:  # Monday=0, Friday=4
            # Return first scheduled time of that day
            return datetime.combine(candidate, min(schedule_times))

    return None


class ScheduledStatusSender:
    """Manages scheduled status sends using threading.Timer."""

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
        self._timer: Optional[threading.Timer] = None
        self._stopped = False

        # Default schedule: 12:30 PM and 4:00 PM
        self.schedule_times = [
            dt_time(12, 30),
            dt_time(16, 0),
        ]

    def start(self) -> None:
        """Start scheduled status sender."""
        if not self.config.telegram_enabled:
            return

        if not self.config.telegram_bot_token or not self.config.telegram_chat_id:
            return

        self._schedule_next()

    def stop(self) -> None:
        """Stop scheduled status sender."""
        self._stopped = True
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def _schedule_next(self) -> None:
        """Schedule next status send."""
        if self._stopped:
            return

        next_time = _next_scheduled_time(self.schedule_times)
        if not next_time:
            return

        now = datetime.now()
        delay_seconds = (next_time - now).total_seconds()

        if delay_seconds <= 0:
            # Should not happen, but guard against it
            delay_seconds = 60

        self._timer = threading.Timer(delay_seconds, self._send_and_reschedule)
        self._timer.daemon = True
        self._timer.start()

    def _send_and_reschedule(self) -> None:
        """Send status and schedule next one."""
        if self._stopped:
            return

        try:
            self._send_status()
        except Exception:
            pass  # Don't crash scheduler on send failure

        # Schedule next run
        self._schedule_next()

    def _send_status(self) -> None:
        """Send scheduled status update."""
        now = datetime.now()
        current_time = now.time()

        # Determine which schedule this is closest to
        closest = min(self.schedule_times, key=lambda t: abs(
            (datetime.combine(now.date(), t) - now).total_seconds()
        ))
        time_label = closest.strftime("%-I:%M %p") if hasattr(closest, "strftime") else closest.strftime("%I:%M %p").lstrip("0")

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
