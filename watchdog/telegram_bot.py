"""Telegram command bot for watchdog.

Runs a python-telegram-bot Application on a dedicated daemon thread with its
own asyncio event loop. The main watchdog loop publishes health + snapshot
into a thread-safe shared state; bot handlers read that state so they never
block on bridge HTTP from the async event loop.

Commands:
    /status - summary of NT connection + strategy state
    /help   - usage
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

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


def format_status(state: _SharedSnapshot) -> str:
    """Render /status reply from a shared-state snapshot. Pure, unit-testable."""
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
    if state.last_cycle_utc:
        lines.append(f"Last cycle: {state.last_cycle_utc}")
    return "\n".join(lines)


def format_help() -> str:
    return (
        "/status  - NT + strategy status\n"
        "/restart - gracefully restart NT and re-enable all strategies\n"
        "/help    - this message"
    )


def format_restart_result(result: Dict[str, Any]) -> str:
    if result.get("ok"):
        stop_mode = str(result.get("stop_mode", "") or "")
        toggled = int(result.get("strategies_toggled", 0) or 0)
        bridge_up = bool(result.get("bridge_up"))
        suffix = "" if stop_mode != "forced" else " (force-killed after soft timeout)"
        bridge_note = "" if bridge_up else " (bridge not yet responsive — strategies enable attempted anyway)"
        return f"✅ NT restarted ({stop_mode or 'ok'}){suffix}. Strategies enabled: {toggled}{bridge_note}"
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
    ) -> None:
        self.config = config
        self.shared_state = shared_state
        self.restart_handler = restart_handler
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
        from telegram import Update
        from telegram.ext import (
            Application,
            CommandHandler,
            MessageHandler,
            filters,
        )

        user_filter = filters.User(user_id=self.config.telegram_allowed_user_ids)

        async def cmd_status(update, context) -> None:  # type: ignore[no-untyped-def]
            snap = self.shared_state.snapshot()
            await update.effective_message.reply_text(format_status(snap))

        async def cmd_help(update, context) -> None:  # type: ignore[no-untyped-def]
            await update.effective_message.reply_text(format_help())

        async def cmd_restart(update, context) -> None:  # type: ignore[no-untyped-def]
            if self.restart_handler is None:
                await update.effective_message.reply_text(
                    "⚠️ /restart not wired (no handler). Check watchdog startup logs."
                )
                return
            await update.effective_message.reply_text(
                "🔧 Restart requested. Gracefully shutting NT — may take up to ~2min…"
            )
            try:
                result = await asyncio.to_thread(self.restart_handler)
            except Exception as exc:  # pragma: no cover - defensive
                await update.effective_message.reply_text(f"❌ Restart failed: {exc!r}")
                return
            await update.effective_message.reply_text(format_restart_result(result or {}))

        async def cmd_unknown(update, context) -> None:  # type: ignore[no-untyped-def]
            await update.effective_message.reply_text(format_help())

        app = (
            Application.builder()
            .token(self.config.telegram_bot_token)
            .build()
        )
        app.add_handler(CommandHandler("status", cmd_status, filters=user_filter))
        app.add_handler(CommandHandler("restart", cmd_restart, filters=user_filter))
        app.add_handler(CommandHandler("help", cmd_help, filters=user_filter))
        # Any other text from a whitelisted user (unknown command or plain text)
        # gets /help. Non-whitelisted users are silently dropped by user_filter.
        app.add_handler(MessageHandler(user_filter & filters.TEXT, cmd_unknown))

        self._application = app
        try:
            await app.initialize()
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
