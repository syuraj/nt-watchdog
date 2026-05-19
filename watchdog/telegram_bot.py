"""Telegram command bot for watchdog.

Runs a python-telegram-bot Application on a dedicated daemon thread with its
own asyncio event loop. The main watchdog loop publishes health + snapshot
into a thread-safe shared state; bot handlers read that state so they never
block on bridge HTTP from the async event loop.

Commands:
    /health - summary of NT connection + strategy state
    /status - account balance + positions
    /errors - Codex review of recent NT/account/watchdog errors
    /restart - restart NT and strategies
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .bridge_client import BridgeClient
from .codex_adhoc import CodexAdhocConfig, CodexAdhocQueue
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
    """Best-effort current local-day execution activity from NT's durable DB."""
    path = db_path or nt_sqlite_path()
    if not path.exists():
        return []
    local_now = now.astimezone() if now is not None else datetime.now().astimezone()
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    count_sql = (
        "select a.Name, count(*), min(e.Time), max(e.Time) "
        "from Executions e "
        "join Accounts a on a.Id = e.Account "
        "where e.Time >= ? and e.Time < ? "
        "group by a.Name "
        "order by a.Name"
    )
    exec_sql = (
        "select a.Name, e.Instrument, e.Time, e.MarketPosition, e.Price, e.Quantity, "
        "mi.PointValue, e.Commission, e.Fee "
        "from Executions e "
        "join Accounts a on a.Id = e.Account "
        "join Instruments i on i.Id = e.Instrument "
        "join MasterInstruments mi on mi.Id = i.MasterInstrument "
        "where e.Time < ? "
        "order by a.Name, e.Instrument, e.Time, e.Id"
    )
    try:
        con = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)
        try:
            start_ticks = _dotnet_ticks(day_start)
            end_ticks = _dotnet_ticks(day_end)
            rows = con.execute(count_sql, (start_ticks, end_ticks)).fetchall()
            exec_rows = con.execute(exec_sql, (end_ticks,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    by_account = {
        str(name or ""): {
            "account": str(name or ""),
            "executions": int(count or 0),
            "has_activity_today": int(count or 0) > 0,
            "sqlite_first_execution_ticks": first_time,
            "sqlite_last_execution_ticks": last_time,
            "activity_source": "sqlite",
        }
        for name, count, first_time, last_time in rows
        if name and int(count or 0) > 0
    }

    pnl_by_account = _estimate_sqlite_realized_pnl(exec_rows, start_ticks)
    for name, pnl_row in pnl_by_account.items():
        target = by_account.get(name)
        if target is None:
            continue
        target.update(pnl_row)
    return list(by_account.values())


def _execution_side(market_position: Any) -> int:
    try:
        value = int(market_position)
    except (TypeError, ValueError):
        return 0
    if value == 0:
        return 1
    if value == 1:
        return -1
    return 0


def _estimate_sqlite_realized_pnl(exec_rows: List[Any], start_ticks: int) -> Dict[str, Dict[str, Any]]:
    lots_by_key: Dict[tuple, List[List[float]]] = {}
    pnl_by_account: Dict[str, Dict[str, Any]] = {}
    for (
        account,
        instrument_id,
        time_ticks,
        market_position,
        price,
        quantity,
        point_value,
        commission,
        fee,
    ) in exec_rows:
        side = _execution_side(market_position)
        try:
            qty_remaining = int(quantity or 0)
            fill_price = float(price or 0.0)
            multiplier = float(point_value or 1.0)
            ticks = int(time_ticks or 0)
            execution_cost = float(commission or 0.0) + float(fee or 0.0)
        except (TypeError, ValueError):
            continue
        if not account or side == 0 or qty_remaining <= 0:
            continue

        cost_per_unit = execution_cost / qty_remaining
        signed_remaining = side * qty_remaining
        key = (str(account), instrument_id)
        lots = lots_by_key.setdefault(key, [])
        while signed_remaining and lots and (lots[0][0] > 0) != (signed_remaining > 0):
            lot_qty, lot_price, lot_cost = lots[0]
            close_qty = min(abs(lot_qty), abs(signed_remaining))
            if lot_qty > 0:
                pnl = (fill_price - lot_price) * close_qty * multiplier
            else:
                pnl = (lot_price - fill_price) * close_qty * multiplier
            entry_cost = (lot_cost / abs(lot_qty)) * close_qty if lot_qty else 0.0
            exit_cost = cost_per_unit * close_qty
            net_pnl = pnl - entry_cost - exit_cost
            if ticks >= start_ticks:
                row = pnl_by_account.setdefault(
                    str(account),
                    {
                        "sqlite_realized_pnl": 0.0,
                        "sqlite_closed_trades": 0,
                        "sqlite_wins": 0,
                        "sqlite_losses": 0,
                    },
                )
                row["sqlite_realized_pnl"] += net_pnl
                row["sqlite_closed_trades"] += 1
                if net_pnl > 0:
                    row["sqlite_wins"] += 1
                elif net_pnl < 0:
                    row["sqlite_losses"] += 1

            if abs(lot_qty) == close_qty:
                lots.pop(0)
            else:
                lots[0][0] = lot_qty - (close_qty if lot_qty > 0 else -close_qty)
                lots[0][2] = lot_cost - entry_cost
            signed_remaining += close_qty if signed_remaining < 0 else -close_qty

        if signed_remaining:
            lots.append([float(signed_remaining), fill_price, abs(signed_remaining) * cost_per_unit])
    return {
        name: {
            **row,
            "sqlite_realized_pnl": round(float(row["sqlite_realized_pnl"]), 2),
        }
        for name, row in pnl_by_account.items()
    }


def merge_daily_activity(sqlite_activity: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build daily rows from SQLite activity."""
    merged: Dict[str, Dict[str, Any]] = {}
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
            fallback_executions = int(row.get("executions") or 0)
        except (TypeError, ValueError):
            fallback_executions = 0
        if fallback_executions > 0:
            target["executions"] = fallback_executions
            target["has_activity_today"] = True
            target["activity_source"] = row.get("activity_source") or "sqlite"
        try:
            sqlite_realized = float(row.get("sqlite_realized_pnl") or 0.0)
        except (TypeError, ValueError):
            sqlite_realized = 0.0
        if abs(sqlite_realized) >= 0.005:
            target["realized_pnl"] = sqlite_realized
            target["total_pnl"] = sqlite_realized
            target["pnl_source"] = "sqlite_estimate"
        try:
            sqlite_closed = int(row.get("sqlite_closed_trades") or 0)
        except (TypeError, ValueError):
            sqlite_closed = 0
        if sqlite_closed > 0:
            target["trades"] = sqlite_closed
            target["wins"] = int(row.get("sqlite_wins") or 0)
            target["losses"] = int(row.get("sqlite_losses") or 0)

    return list(merged.values())


def format_health(state: _SharedSnapshot) -> str:
    """Render /health reply from a shared-state snapshot. Pure, unit-testable."""
    health = state.health or {}
    snap = state.runtime_snapshot or {}

    status = str(health.get("status") or "unknown")
    conn = health.get("connections") or {}
    total = conn.get("total", "?")
    connected = conn.get("connected", "?")

    strat_info = snap.get("strategy_runtime") or {}
    strategies = sorted(
        strat_info.get("strategies") or [],
        key=lambda s: (
            str(s.get("name") or "").casefold() if isinstance(s, dict) else "",
            str(s.get("account") or "").casefold() if isinstance(s, dict) else "",
        ),
    )
    accounts = snap.get("accounts") or []
    cash_by_account = {
        str(a.get("name") or ""): a.get("cash")
        for a in accounts
        if isinstance(a, dict)
    }
    active = sum(1 for s in strategies if _strategy_is_active(s))
    total_strats = len(strategies)
    any_inactive_strategy = any(not _strategy_is_active(s) for s in strategies)

    if not (isinstance(connected, int) and connected > 0 and status == "ok"):
        dot = "\U0001F534"
    elif any_inactive_strategy:
        dot = "\U0001F7E1"
    else:
        dot = "\U0001F7E2"

    lines: List[str] = []
    lines.append(f"{dot} NT connections: {connected}/{total}")
    lines.append(f"Strategies: {active} active / {total_strats} total")
    for s in strategies:
        name = str(s.get("name") or "?")
        account = str(s.get("account") or "?")
        account_value = _fmt_money(cash_by_account.get(account)) if account in cash_by_account else "?"
        state_str = str(s.get("state") or "")
        details = [account_value]
        if not _strategy_is_active(s):
            status_parts = []
            if not bool(s.get("is_enabled")):
                status_parts.append("off")
            if state_str and state_str.lower() not in {"active", "realtime"}:
                status_parts.append(state_str)
            if status_parts:
                details.append("/".join(status_parts))
        lines.append(f" \u2022 {name} ({', '.join(details)})")
    lines.append(f"Health: {status}")
    reasons = health.get("reasons") or []
    if isinstance(reasons, list) and reasons:
        lines.append(f"Reasons: {', '.join(str(r) for r in reasons)}")
    return "\n".join(lines)


def format_status(
    state: _SharedSnapshot,
    daily_activity: List[Dict[str, Any]],
    positions_override: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Render /status reply: balance + positions + today's P&L. Pure, unit-testable.

    If positions_override is provided, it replaces the cached snapshot's positions
    (use for live fetches to avoid stale open-position lines after a close).
    """
    snap = state.runtime_snapshot or {}
    if not snap:
        return "No snapshot yet - watchdog may still be starting."

    accounts = snap.get("accounts") or []
    positions = positions_override if positions_override is not None else (snap.get("positions") or [])

    pnl_by_account: Dict[str, Dict[str, Any]] = {}
    for row in daily_activity or []:
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
        total_value = _as_float(row.get("total_pnl"))
        total = _fmt_money(row.get("total_pnl"))
        trades = _as_int(row.get("trades"))
        wins = _as_int(row.get("wins"))
        losses = _as_int(row.get("losses"))
        account_positions = positions_by_account.get(name, [])
        has_closed_or_realized_pnl = (
            trades > 0
            or abs(total_value) >= 0.005
            or abs(_as_float(row.get("realized_pnl"))) >= 0.005
        )
        acc_lines = [
            f"\U0001F4B0 {name} ({cash})",
        ]
        if has_closed_or_realized_pnl or not account_positions:
            acc_lines.append(
                f"  {_pnl_dot(total_value)} Today: {total} - {trades} closed ({wins}W/{losses}L)"
            )
        for p in account_positions:
            instr = str(p.get("instrument") or "?")
            side = str(p.get("side") or "?")
            qty = p.get("quantity", "?")
            unreal_value = _as_float(p.get("unrealized"))
            unreal = _fmt_money(p.get("unrealized"))
            acc_lines.append(f"  {_pnl_dot(unreal_value)} {instr} {side} {qty} (unreal {unreal})")
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


def _pnl_dot(val: Any) -> str:
    try:
        n = float(val)
    except (TypeError, ValueError):
        n = 0.0
    return "\U0001F534" if n < -0.005 else "\U0001F7E2"


def _strategy_is_active(strategy: Dict[str, Any]) -> bool:
    if not bool(strategy.get("is_enabled")):
        return False
    state = str(strategy.get("state") or "").lower()
    return not state or state in {"active", "realtime"}


def format_commands() -> str:
    return (
        "/health  - NT + strategy status\n"
        "/status  - account balance + positions\n"
        "/errors  - review recent NT/account/watchdog errors\n"
        "/restart - restart NT and all strategies"
    )


def format_restart_result(result: Dict[str, Any]) -> str:
    if result.get("ok"):
        toggled = int(result.get("strategies_toggled", 0) or 0)
        bridge_up = bool(result.get("bridge_up"))
        bridge_note = "" if bridge_up else " (bridge not yet responsive - strategies enable attempted anyway)"
        return f"\u2705 NT restarted. Strategies enabled: {toggled}{bridge_note}"
    err = str(result.get("error", "") or "unknown_error")
    return f"\u274c Restart failed: {err}"


async def await_with_typing(
    awaitable: Awaitable[str],
    send_typing: Callable[[], Awaitable[None]],
    interval_sec: float = 4.0,
) -> str:
    task = asyncio.create_task(awaitable)
    try:
        while not task.done():
            try:
                await send_typing()
            except Exception:
                pass
            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout=interval_sec)
            except asyncio.TimeoutError:
                continue
        return await task
    finally:
        if not task.done():
            task.cancel()


def build_errors_review_prompt(args_text: str = "") -> str:
    window = args_text.strip() or "since local midnight yesterday"
    return "\n".join(
        [
            f"/errors command: review recent NinjaTrader/account/watchdog errors for {window}.",
            "",
            "Scope:",
            "- Check NinjaTrader log errors, exceptions, order/account/execution errors, connection issues, blocking windows, HealthBridge health, watchdog recovery failures, Telegram/send failures, and stale UI/main-thread symptoms.",
            "- Use prefetched live health/runtime context first, then readable watchdog/NT logs if needed.",
            "- Separate confirmed current problems from historical resolved events.",
            "- Include concrete timestamps, log file paths/line references, account names, instruments, order IDs, and endpoint fields when available.",
            "- If no errors are found, say that clearly and name the scan window/evidence used.",
            "- Keep the Telegram answer concise.",
        ]
    )


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
        codex_queue: Optional[CodexAdhocQueue] = None,
    ) -> None:
        self.config = config
        self.shared_state = shared_state
        self.restart_handler = restart_handler
        self.bridge_client = bridge_client
        if codex_queue is not None:
            self.codex_queue = codex_queue
        elif config.telegram_adhoc_codex_enabled:
            self.codex_queue = CodexAdhocQueue(
                CodexAdhocConfig(
                    command=config.telegram_adhoc_codex_command,
                    workdir=config.telegram_adhoc_codex_workdir,
                    data_dir=config.telegram_adhoc_codex_data_dir,
                    timeout_sec=config.telegram_adhoc_codex_timeout_sec,
                    queue_max=config.telegram_adhoc_codex_queue_max,
                    max_reply_chars=config.telegram_adhoc_codex_max_reply_chars,
                    bridge_url=config.bridge_url,
                    health_endpoint=config.health_endpoint,
                    runtime_snapshot_endpoint=config.runtime_snapshot_endpoint,
                    events_log_path=config.events_log_path,
                )
            )
        else:
            self.codex_queue = None
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
        from telegram.constants import ChatAction
        from telegram.ext import (
            Application,
            CommandHandler,
            MessageHandler,
            filters,
        )

        user_filter = filters.User(user_id=self.config.telegram_allowed_user_ids)

        async def cmd_health(update, context) -> None:  # type: ignore[no-untyped-def]
            snap = self.shared_state.snapshot()
            try:
                await update.effective_message.reply_text(format_health(snap))
            except Exception:
                pass  # app shutting down, user won't see reply anyway

        async def cmd_status(update, context) -> None:  # type: ignore[no-untyped-def]
            snap = self.shared_state.snapshot()
            if self.bridge_client is None:
                daily: List[Dict[str, Any]] = []
                positions: Optional[List[Dict[str, Any]]] = None
            else:
                runtime, positions, sqlite_activity = await asyncio.gather(
                    asyncio.to_thread(self.bridge_client.safe_runtime_snapshot),
                    asyncio.to_thread(self.bridge_client.safe_positions),
                    asyncio.to_thread(load_sqlite_daily_activity),
                )
                daily = merge_daily_activity(sqlite_activity)
                snap = snapshot_with_runtime(snap, runtime)
            try:
                await update.effective_message.reply_text(
                    format_status(snap, daily, positions_override=positions)
                )
            except Exception:
                pass  # app shutting down, user won't see reply anyway

        async def cmd_restart(update, context) -> None:  # type: ignore[no-untyped-def]
            if self.restart_handler is None:
                try:
                    await update.effective_message.reply_text(
                        "\u26a0\ufe0f /restart not wired (no handler). Check watchdog startup logs."
                    )
                except Exception:
                    pass
                return
            try:
                await update.effective_message.reply_text(
                    "\U0001F527 Restart requested. Forcefully shutting NT - may take up to ~2min..."
                )
            except Exception:
                pass
            try:
                result = await asyncio.to_thread(self.restart_handler)
            except Exception as exc:  # pragma: no cover - defensive
                try:
                    await update.effective_message.reply_text(f"\u274c Restart failed: {exc!r}")
                except Exception:
                    pass
                return
            try:
                await update.effective_message.reply_text(format_restart_result(result or {}))
            except Exception:
                pass  # app shutting down, user won't see reply anyway

        async def cmd_errors(update, context) -> None:  # type: ignore[no-untyped-def]
            message = update.effective_message
            if self.codex_queue is None:
                try:
                    await message.reply_text("Codex error review is disabled.")
                except Exception:
                    pass
                return
            args_text = " ".join(str(part) for part in getattr(context, "args", []) or [])
            user_id = int(getattr(update.effective_user, "id", 0) or 0)

            async def send_typing() -> None:
                chat = update.effective_chat
                if chat is not None:
                    await context.bot.send_chat_action(
                        chat_id=chat.id,
                        action=ChatAction.TYPING,
                    )

            answer = await await_with_typing(
                self.codex_queue.ask(user_id, build_errors_review_prompt(args_text)),
                send_typing,
            )
            try:
                await message.reply_text(answer)
            except Exception:
                pass  # app shutting down, user won't see reply anyway

        async def cmd_unknown(update, context) -> None:  # type: ignore[no-untyped-def]
            message = update.effective_message
            text = str(getattr(message, "text", "") or "").strip()
            if not text:
                return
            if self.codex_queue is None:
                try:
                    await message.reply_text(format_commands())
                except Exception:
                    pass
                return
            user_id = int(getattr(update.effective_user, "id", 0) or 0)

            async def send_typing() -> None:
                chat = update.effective_chat
                if chat is not None:
                    await context.bot.send_chat_action(
                        chat_id=chat.id,
                        action=ChatAction.TYPING,
                    )

            answer = await await_with_typing(self.codex_queue.ask(user_id, text), send_typing)
            try:
                await message.reply_text(answer)
            except Exception:
                pass  # app shutting down, user won't see reply anyway

        app = (
            Application.builder()
            .token(self.config.telegram_bot_token)
            .build()
        )
        app.add_handler(CommandHandler("health", cmd_health, filters=user_filter))
        app.add_handler(CommandHandler("status", cmd_status, filters=user_filter))
        app.add_handler(CommandHandler("errors", cmd_errors, filters=user_filter))
        app.add_handler(CommandHandler("restart", cmd_restart, filters=user_filter))
        # Any other text from a whitelisted user (unknown command or plain text)
        # is treated as an ad hoc Codex question. Non-whitelisted users are
        # silently dropped by user_filter.
        app.add_handler(MessageHandler(user_filter & filters.TEXT, cmd_unknown))

        self._application = app
        try:
            await app.initialize()
            await app.bot.set_my_commands([
                BotCommand("health", "NT + strategy status"),
                BotCommand("status", "Account balance + positions"),
                BotCommand("errors", "Review errors"),
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
