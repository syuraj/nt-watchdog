"""Telegram command bot for watchdog.

Runs a python-telegram-bot Application on a dedicated daemon thread with its
own asyncio event loop. The main watchdog loop publishes health + snapshot
into a thread-safe shared state; bot handlers read that state so they never
block on bridge HTTP from the async event loop.

Commands:
    /health - summary of NT connection + strategy state
    /status - account balance + positions
    /restart - restart NT and strategies
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .bridge_client import BridgeClient
from .config import WatchdogConfig


@dataclass
class _SharedSnapshot:
    health: Dict[str, Any] = field(default_factory=dict)
    runtime_snapshot: Dict[str, Any] = field(default_factory=dict)
    last_cycle_utc: str = ""
    started_utc: str = ""


class TelegramSharedState:
    """Thread-safe publish/read of latest watchdog state for bot handlers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data = _SharedSnapshot(started_utc=_iso_now())

    def publish(self, health: Dict[str, Any], runtime_snapshot: Dict[str, Any]) -> None:
        with self._lock:
            self._data.health = dict(health) if isinstance(health, dict) else {}
            self._data.runtime_snapshot = (
                dict(runtime_snapshot) if isinstance(runtime_snapshot, dict) else {}
            )
            self._data.last_cycle_utc = _iso_now()

    def snapshot(self) -> _SharedSnapshot:
        with self._lock:
            return _SharedSnapshot(
                health=dict(self._data.health),
                runtime_snapshot=dict(self._data.runtime_snapshot),
                last_cycle_utc=self._data.last_cycle_utc,
                started_utc=self._data.started_utc,
            )


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dotnet_ticks(value: datetime) -> int:
    epoch = datetime(1, 1, 1, tzinfo=timezone.utc)
    return int((value.astimezone(timezone.utc) - epoch).total_seconds() * 10000000)


def nt_sqlite_path() -> Path:
    return Path.home() / "Documents" / "NinjaTrader 8" / "db" / "NinjaTrader.sqlite"


def load_sqlite_daily_activity(
    db_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Best-effort current local-day execution counts from NT's durable DB."""
    path = db_path or nt_sqlite_path()
    if not path.exists():
        return []
    local_now = now.astimezone() if now is not None else datetime.now().astimezone()
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    sql = (
        "select a.Name, count(*), min(e.Time), max(e.Time) "
        "from Executions e "
        "join Accounts a on a.Id = e.Account "
        "where e.Time >= ? and e.Time < ? "
        "group by a.Name "
        "order by a.Name"
    )
    try:
        con = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)
        try:
            rows = con.execute(sql, (_dotnet_ticks(day_start), _dotnet_ticks(day_end))).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    return [
        {
            "account": str(name or ""),
            "executions": int(count or 0),
            "has_activity_today": int(count or 0) > 0,
            "sqlite_first_execution_ticks": first_time,
            "sqlite_last_execution_ticks": last_time,
            "activity_source": "sqlite",
        }
        for name, count, first_time, last_time in rows
        if name and int(count or 0) > 0
    ]


def merge_daily_activity(
    daily_pnl: List[Dict[str, Any]],
    sqlite_activity: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Add SQLite execution evidence when bridge in-memory counts are empty."""
    merged: Dict[str, Dict[str, Any]] = {}
    for row in daily_pnl or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("account") or "")
        if name:
            merged[name] = dict(row)

    for row in sqlite_activity or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("account") or "")
        if not name:
            continue
        target = merged.setdefault(
            name,
            {
                "account": name,
                "total_pnl": 0.0,
                "trades": 0,
                "wins": 0,
                "losses": 0,
            },
        )
        try:
            existing_executions = int(target.get("executions") or 0)
        except (TypeError, ValueError):
            existing_executions = 0
        try:
            fallback_executions = int(row.get("executions") or 0)
        except (TypeError, ValueError):
            fallback_executions = 0
        if existing_executions <= 0 and fallback_executions > 0:
            target["executions"] = fallback_executions
            target["has_activity_today"] = True
            target["activity_source"] = row.get("activity_source") or "sqlite"

    return list(merged.values())


def format_health(state: _SharedSnapshot) -> str:
    """Render /health reply from a shared-state snapshot. Pure, unit-testable."""
    health = state.health or {}
    snap = state.runtime_snapshot or {}

    status = str(health.get("status") or "unknown")
    conn = health.get("connections") or {}
    total = conn.get("total", "?")
    connected = conn.get("connected", "?")

    dot = "🟢" if isinstance(connected, int) and connected > 0 and status == "ok" else "🔴"

    strat_info = snap.get("strategy_runtime") or {}
    strategies = strat_info.get("strategies") or []
    active = sum(1 for s in strategies if s.get("is_enabled"))
    total_strats = len(strategies)

    lines: List[str] = []
    lines.append(f"{dot} NT connections: {connected}/{total}")
    lines.append(f"Strategies: {active} active / {total_strats} total")
    for s in strategies:
        name = str(s.get("name") or "?")
        is_on = bool(s.get("is_enabled"))
        state_str = str(s.get("state") or "")
        marker = "active" if is_on else "off"
        if state_str and state_str.lower() not in {"active", "realtime"}:
            marker = f"{marker}/{state_str}"
        lines.append(f" • {name} ({marker})")
    lines.append(f"Health: {status}")
    reasons = health.get("reasons") or []
    if isinstance(reasons, list) and reasons:
        lines.append(f"Reasons: {', '.join(str(r) for r in reasons)}")
    return "\n".join(lines)


def format_status(
    state: _SharedSnapshot,
    daily_pnl: List[Dict[str, Any]],
    positions_override: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Render /status reply: balance + positions + today's P&L. Pure, unit-testable.

    If positions_override is provided, it replaces the cached snapshot's positions
    (use for live fetches to avoid stale open-position lines after a close).
    """
    snap = state.runtime_snapshot or {}
    if not snap:
        return "No snapshot yet — watchdog may still be starting."

    accounts = snap.get("accounts") or []
    positions = positions_override if positions_override is not None else (snap.get("positions") or [])

    pnl_by_account: Dict[str, Dict[str, Any]] = {}
    for row in daily_pnl or []:
        if not isinstance(row, dict):
            continue
        pnl_by_account[str(row.get("account") or "")] = row  # last wins on dupes

    positions_by_account: Dict[str, List[Dict[str, Any]]] = {}
    for p in positions:
        if not isinstance(p, dict):
            continue
        positions_by_account.setdefault(str(p.get("account") or ""), []).append(p)

    def _as_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _as_float(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _as_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _is_active(name: str) -> bool:
        row = pnl_by_account.get(name) or {}
        if _as_bool(row.get("has_activity_today")):
            return True
        if _as_int(row.get("trades")) > 0:
            return True
        if _as_int(row.get("executions")) > 0:
            return True
        if abs(_as_float(row.get("total_pnl"))) >= 0.005:
            return True
        if abs(_as_float(row.get("realized_pnl"))) >= 0.005:
            return True
        if abs(_as_float(row.get("unrealized_pnl"))) >= 0.005:
            return True
        return bool(positions_by_account.get(name))

    active = [
        a for a in accounts
        if a.get("connected") and _is_active(str(a.get("name") or ""))
    ]

    blocks: List[str] = []
    if not active:
        blocks.append("No accounts traded or holding positions today.")
    for acc in active:
        name = str(acc.get("name") or "?")
        cash = _fmt_money(acc.get("cash"))
        row = pnl_by_account.get(name) or {}
        total = _fmt_money(row.get("total_pnl"))
        trades = _as_int(row.get("trades"))
        executions = _as_int(row.get("executions"))
        wins = _as_int(row.get("wins"))
        losses = _as_int(row.get("losses"))
        acc_lines = [
            f"💰 {name}",
            f"  Cash: {cash}",
            f"  Today: {total} · {trades} closed ({wins}W/{losses}L)",
        ]
        if trades == 0 and executions > 0:
            acc_lines.append(f"  Activity: {executions} executions")
        for p in positions_by_account.get(name, []):
            instr = str(p.get("instrument") or "?")
            side = str(p.get("side") or "?")
            qty = p.get("quantity", "?")
            avg = _fmt_money(p.get("avg_price"))
            unreal = _fmt_money(p.get("unrealized"))
            acc_lines.append(f"  🟢 {instr} {side} {qty} @ {avg} (unreal {unreal})")
        blocks.append("\n".join(acc_lines))

    return "\n\n".join(blocks)


def snapshot_with_runtime(state: _SharedSnapshot, runtime_snapshot: Dict[str, Any]) -> _SharedSnapshot:
    """Return a status snapshot using a freshly fetched bridge runtime payload."""
    if not isinstance(runtime_snapshot, dict) or runtime_snapshot.get("error"):
        return state
    return _SharedSnapshot(
        health=dict(runtime_snapshot.get("health") or state.health or {}),
        runtime_snapshot=dict(runtime_snapshot),
        last_cycle_utc=state.last_cycle_utc,
        started_utc=state.started_utc,
    )


def _fmt_money(val: Any) -> str:
    try:
        n = float(val)
    except (TypeError, ValueError):
        return "$?"
    sign = "-" if n < 0 else ""
    return f"{sign}${abs(n):,.2f}"


def format_commands() -> str:
    return (
        "/health  - NT + strategy status\n"
        "/status  - account balance + positions\n"
        "/restart - restart NT and all strategies"
    )


def format_restart_result(result: Dict[str, Any]) -> str:
    if result.get("ok"):
        toggled = int(result.get("strategies_toggled", 0) or 0)
        bridge_up = bool(result.get("bridge_up"))
        bridge_note = "" if bridge_up else " (bridge not yet responsive — strategies enable attempted anyway)"
        return f"✅ NT restarted. Strategies enabled: {toggled}{bridge_note}"
    err = str(result.get("error", "") or "unknown_error")
    return f"❌ Restart failed: {err}"


class TelegramBotService:
    """Manages the PTB Application lifecycle on a daemon thread.

    Usage:
        service = TelegramBotService(config, shared_state)
        service.start()   # launches thread; non-blocking, safe on bad token
        ...
        service.stop()    # signals shutdown, joins thread
    """

    def __init__(
        self,
        config: WatchdogConfig,
        shared_state: TelegramSharedState,
        restart_handler: Optional[Callable[[], Dict[str, Any]]] = None,
        bridge_client: Optional[BridgeClient] = None,
    ) -> None:
        self.config = config
        self.shared_state = shared_state
        self.restart_handler = restart_handler
        self.bridge_client = bridge_client
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._application = None
        self._stop_event = threading.Event()
        self.last_error = ""

    @property
    def enabled(self) -> bool:
        return bool(
            self.config.telegram_enabled
            and self.config.telegram_bot_token
            and self.config.telegram_allowed_user_ids
        )

    def start(self) -> bool:
        if not self.enabled:
            self.last_error = "telegram command bot disabled (missing token or allowed_user_ids)"
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._thread = threading.Thread(
            target=self._run_thread,
            name="TelegramBotThread",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self, timeout_sec: float = 5.0) -> None:
        if self._thread is None or self._loop is None:
            return
        self._stop_event.set()
        try:
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        except Exception:
            pass
        self._thread.join(timeout=timeout_sec)

    async def _shutdown(self) -> None:
        app = self._application
        if app is None:
            return
        try:
            if app.updater and app.updater.running:
                await app.updater.stop()
            if app.running:
                await app.stop()
            await app.shutdown()
        except Exception as exc:
            self.last_error = f"shutdown: {exc!r}"

    def _run_thread(self) -> None:
        try:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._run_async())
        except Exception as exc:
            self.last_error = f"thread: {exc!r}"
        finally:
            try:
                if self._loop is not None:
                    self._loop.close()
            except Exception:
                pass

    async def _run_async(self) -> None:
        # Import PTB lazily so watchdog can still run if the dep is missing.
        from telegram import BotCommand, Update
        from telegram.ext import (
            Application,
            CommandHandler,
            MessageHandler,
            filters,
        )

        user_filter = filters.User(user_id=self.config.telegram_allowed_user_ids)

        async def cmd_health(update, context) -> None:  # type: ignore[no-untyped-def]
            snap = self.shared_state.snapshot()
            await update.effective_message.reply_text(format_health(snap))

        async def cmd_status(update, context) -> None:  # type: ignore[no-untyped-def]
            snap = self.shared_state.snapshot()
            if self.bridge_client is None:
                daily: List[Dict[str, Any]] = []
                positions: Optional[List[Dict[str, Any]]] = None
            else:
                runtime, daily, positions, sqlite_activity = await asyncio.gather(
                    asyncio.to_thread(self.bridge_client.safe_runtime_snapshot),
                    asyncio.to_thread(self.bridge_client.safe_daily_pnl),
                    asyncio.to_thread(self.bridge_client.safe_positions),
                    asyncio.to_thread(load_sqlite_daily_activity),
                )
                daily = merge_daily_activity(daily, sqlite_activity)
                snap = snapshot_with_runtime(snap, runtime)
            await update.effective_message.reply_text(
                format_status(snap, daily, positions_override=positions)
            )

        async def cmd_restart(update, context) -> None:  # type: ignore[no-untyped-def]
            if self.restart_handler is None:
                await update.effective_message.reply_text(
                    "⚠️ /restart not wired (no handler). Check watchdog startup logs."
                )
                return
            await update.effective_message.reply_text(
                "🔧 Restart requested. Forcefully shutting NT — may take up to ~2min…"
            )
            try:
                result = await asyncio.to_thread(self.restart_handler)
            except Exception as exc:  # pragma: no cover - defensive
                await update.effective_message.reply_text(f"❌ Restart failed: {exc!r}")
                return
            await update.effective_message.reply_text(format_restart_result(result or {}))

        async def cmd_unknown(update, context) -> None:  # type: ignore[no-untyped-def]
            await update.effective_message.reply_text(format_commands())

        app = (
            Application.builder()
            .token(self.config.telegram_bot_token)
            .build()
        )
        app.add_handler(CommandHandler("health", cmd_health, filters=user_filter))
        app.add_handler(CommandHandler("status", cmd_status, filters=user_filter))
        app.add_handler(CommandHandler("restart", cmd_restart, filters=user_filter))
        # Any other text from a whitelisted user (unknown command or plain text)
        # gets /help. Non-whitelisted users are silently dropped by user_filter.
        app.add_handler(MessageHandler(user_filter & filters.TEXT, cmd_unknown))

        self._application = app
        try:
            await app.initialize()
            await app.bot.set_my_commands([
                BotCommand("health", "NT + strategy status"),
                BotCommand("status", "Account balance + positions"),
                BotCommand("restart", "Restart NT and strategies"),
            ])
            await app.start()
            await app.updater.start_polling(
                drop_pending_updates=True,
                allowed_updates=[Update.MESSAGE],
            )
            # Park until stop requested. Poll the stop event on the loop.
            while not self._stop_event.is_set():
                await asyncio.sleep(0.5)
        except Exception as exc:
            self.last_error = f"runtime: {exc!r}"
        finally:
            await self._shutdown()
