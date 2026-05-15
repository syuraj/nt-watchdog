"""Scheduled log scanner for errors, API failures, and NT freeze indicators.

Scans NT logs and watchdog event logs for urgent issues not caught by real-time monitoring.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Set

from apscheduler.schedulers.background import BackgroundScheduler

from .config import WatchdogConfig
from .telegram_notifier import TelegramNotifier


# Error patterns indicating urgent issues
URGENT_PATTERNS = [
    # NT crashes/freezes
    (r"application.*crash", "NT crash detected"),
    (r"unhandled exception", "Unhandled exception in NT"),
    (r"deadlock detected", "Deadlock detected"),
    (r"main.*thread.*not responding", "Main thread freeze"),

    # Connection failures
    (r"connection.*failed.*repeatedly", "Repeated connection failures"),
    (r"unable to connect.*timeout", "Connection timeout"),
    (r"api.*authentication.*failed", "API auth failure"),

    # Data issues
    (r"market data.*disconnected", "Market data disconnection"),
    (r"order.*rejected", "Order rejected"),
    (r"insufficient.*margin", "Margin issue"),

    # HealthBridge issues
    (r"healthbridge.*failed to start", "HealthBridge startup failure"),
    (r"bridge.*unresponsive", "Bridge unresponsive"),
]


class ScheduledLogScanner:
    """Scans logs periodically for urgent issues."""

    def __init__(self, config: WatchdogConfig, notifier: TelegramNotifier):
        self.config = config
        self.notifier = notifier
        self.scheduler = BackgroundScheduler()
        self.last_scan_time = datetime.now() - timedelta(hours=1)
        self.alerted_issues: Set[str] = set()  # Deduplication
        self.alert_cooldown_sec = 1800  # 30 min cooldown per issue type

    def start(self) -> None:
        """Start scheduled log scanner."""
        if not self.config.telegram_enabled:
            return

        if not self.config.telegram_bot_token or not self.config.telegram_chat_id:
            return

        # Scan every 15 minutes
        self.scheduler.add_job(
            self._scan_logs,
            "cron",
            minute="*/15",
        )

        self.scheduler.start()

    def stop(self) -> None:
        """Stop scheduled log scanner."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def _scan_logs(self) -> None:
        """Scan logs for urgent issues."""
        now = datetime.now()
        issues: List[Dict[str, Any]] = []

        # Scan NT logs
        issues.extend(self._scan_nt_logs(now))

        # Scan watchdog event log
        issues.extend(self._scan_watchdog_events(now))

        # Group by type and filter already alerted
        grouped = defaultdict(list)
        for issue in issues:
            issue_type = issue["type"]
            grouped[issue_type].append(issue)

        # Send alerts for new urgent issues
        alerts_sent = 0
        for issue_type, issue_list in grouped.items():
            if self._should_alert(issue_type, now):
                self._send_alert(issue_type, issue_list)
                self.alerted_issues.add(f"{issue_type}:{now.timestamp()}")
                alerts_sent += 1

        # Clean old entries from alerted_issues (older than cooldown)
        cutoff = now.timestamp() - self.alert_cooldown_sec
        self.alerted_issues = {
            entry for entry in self.alerted_issues
            if float(entry.split(":")[-1]) > cutoff
        }

        self.last_scan_time = now

    def _scan_nt_logs(self, now: datetime) -> List[Dict[str, Any]]:
        """Scan NinjaTrader log files for errors."""
        issues = []
        nt_log_dir = Path.home() / "Documents" / "NinjaTrader 8" / "log"

        if not nt_log_dir.exists():
            return issues

        # Scan log files modified since last scan
        cutoff = now - timedelta(minutes=20)  # Overlap for safety
        for log_file in nt_log_dir.glob("log.*.txt"):
            try:
                if datetime.fromtimestamp(log_file.stat().st_mtime) < cutoff:
                    continue

                with log_file.open("r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        for pattern, description in URGENT_PATTERNS:
                            if re.search(pattern, line, re.IGNORECASE):
                                issues.append({
                                    "type": description,
                                    "source": "nt_log",
                                    "file": log_file.name,
                                    "line": line.strip()[:200],
                                    "timestamp": now.isoformat(),
                                })
            except Exception:
                pass  # Skip unreadable logs

        return issues

    def _scan_watchdog_events(self, now: datetime) -> List[Dict[str, Any]]:
        """Scan watchdog event log for failures."""
        issues = []
        events_path = Path(self.config.events_log_path)

        if not events_path.exists():
            return issues

        # Scan recent events (last 20 minutes)
        cutoff = now - timedelta(minutes=20)

        try:
            with events_path.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        event = json.loads(line)
                        event_time = datetime.fromisoformat(
                            event.get("timestamp", "").replace("Z", "+00:00")
                        )

                        if event_time < cutoff:
                            continue

                        # Check for alert send failures
                        if event.get("alert_sent") is False:
                            issues.append({
                                "type": "Alert send failure",
                                "source": "watchdog_events",
                                "details": event.get("alert_error", ""),
                                "timestamp": event.get("timestamp"),
                            })

                        # Check for repeated recovery failures
                        if event.get("action") == "restart_nt" and event.get("reason") == "reconnect_exhausted":
                            issues.append({
                                "type": "Recovery escalation",
                                "source": "watchdog_events",
                                "details": "Escalated to NT restart after reconnect failures",
                                "timestamp": event.get("timestamp"),
                            })

                        # Check for process start failures
                        if event.get("kind") == "process_start" and event.get("status") == "failed":
                            issues.append({
                                "type": "NT startup failure",
                                "source": "watchdog_events",
                                "details": event.get("reason", ""),
                                "timestamp": event.get("timestamp"),
                            })

                    except (json.JSONDecodeError, ValueError):
                        pass
        except Exception:
            pass

        return issues

    def _should_alert(self, issue_type: str, now: datetime) -> bool:
        """Check if we should alert for this issue type (cooldown + deduplication)."""
        cutoff = now.timestamp() - self.alert_cooldown_sec

        for entry in self.alerted_issues:
            if entry.startswith(f"{issue_type}:"):
                timestamp = float(entry.split(":")[-1])
                if timestamp > cutoff:
                    return False  # Still in cooldown

        return True

    def _send_alert(self, issue_type: str, issues: List[Dict[str, Any]]) -> None:
        """Send Telegram alert for urgent issues."""
        count = len(issues)

        # Build alert message
        header = f"🚨 Log Scanner Alert: {issue_type}\n"
        header += f"Found {count} occurrence(s) in last 15 min\n\n"

        # Include sample details (up to 3)
        samples = []
        for issue in issues[:3]:
            source = issue.get("source", "")
            if "line" in issue:
                samples.append(f"• {issue['file']}: {issue['line'][:150]}")
            elif "details" in issue:
                samples.append(f"• {issue['details'][:150]}")

        if samples:
            details = "\n".join(samples)
            if count > 3:
                details += f"\n... and {count - 3} more"
        else:
            details = "Check logs for details"

        message = header + details

        # Send alert
        self.notifier.send_message(message)
