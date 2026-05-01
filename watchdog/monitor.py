from __future__ import annotations

import argparse
import msvcrt
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .bridge_client import BridgeClient
from .config import WatchdogConfig, load_config
from .nt_process import NTProcessManager
from .recovery import RecoveryManager
from .state_store import StateStore
from .telegram_bot import TelegramBotService, TelegramSharedState
from .telegram_notifier import TelegramNotifier


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


class SingleInstanceLock:
    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self._fh = None

    def acquire(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.lock_path.open("a+", encoding="utf-8")
        try:
            self._fh.seek(0, os.SEEK_END)
            if self._fh.tell() == 0:
                self._fh.write("0")
                self._fh.flush()
            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write(str(os.getpid()))
            self._fh.flush()
            return True
        except OSError:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
            return False

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None


def run_watchdog(config: WatchdogConfig, max_cycles: int = 0) -> None:
    lock_path = Path(config.snapshot_path).with_name("watchdog.lock")
    single_lock = SingleInstanceLock(lock_path)
    if not single_lock.acquire():
        print(f"[{_now()}] watchdog already running; exiting.")
        return

    bridge = BridgeClient(config)
    state_store = StateStore(config)
    process_manager = NTProcessManager(config)
    notifier = TelegramNotifier(config)
    recovery = RecoveryManager(
        config=config,
        bridge=bridge,
        process_manager=process_manager,
        state_store=state_store,
        notifier=notifier,
    )

    telegram_state = TelegramSharedState()
    telegram_bot = TelegramBotService(
        config,
        telegram_state,
        restart_handler=recovery.manual_restart,
        bridge_client=bridge,
    )
    try:
        if telegram_bot.start():
            print(f"[{_now()}] telegram command bot started")
        elif telegram_bot.last_error:
            print(f"[{_now()}] telegram command bot not started: {telegram_bot.last_error}")
    except Exception as exc:
        print(f"[{_now()}] telegram command bot failed to start: {exc}")

    try:
        started = time.time()
        print(f"[{_now()}] watchdog started. bridge={config.bridge_url}")
        # Clear stale per-incident counters from a prior run. Without this, a crashed
        # watchdog's leftover reconnect_failures can push the first post-restart cycle
        # straight into the restart-escalation branch, killing NT without ever trying
        # a reconnect first.
        try:
            stale_state = state_store.load_runtime_state()
            if stale_state.get("reconnect_failures") or stale_state.get("last_incident_id"):
                stale_state["reconnect_failures"] = 0
                stale_state["last_incident_id"] = ""
                state_store.save_runtime_state(stale_state)
                print(f"[{_now()}] cleared stale incident state from prior run")
        except Exception as exc:
            print(f"[{_now()}] warn: could not clear stale state: {exc}")
        cycle_num = 0
        while True:
            cycle_num += 1
            process_running = process_manager.is_running()
            health = bridge.safe_health()
            runtime_snapshot = bridge.safe_runtime_snapshot()

            # Bootstrapping: if NT process is down outside startup grace, start it.
            if not process_running and (time.time() - started) > config.startup_grace_sec:
                if process_manager.start():
                    state_store.append_event({"kind": "process_start", "status": "success", "reason": "process_not_running"})
                    # Route through recovery._notify so the event log captures
                    # alert success + last_error, matching the rest of the flow.
                    recovery._notify(
                        "process_started",
                        {"status": "recovering", "action": "start_nt", "reason": "process_not_running"},
                        incident_id=uuid4().hex[:10],
                    )
                    time.sleep(max(5, config.startup_grace_sec))
                    continue
                state_store.append_event({"kind": "process_start", "status": "failed", "reason": "process_not_running"})

            result = recovery.handle_cycle(health=health, runtime_snapshot=runtime_snapshot)
            conn = health.get("connections", {}) if isinstance(health, dict) else {}
            conn_total = conn.get("total", "?") if isinstance(conn, dict) else "?"
            conn_connected = conn.get("connected", "?") if isinstance(conn, dict) else "?"
            nt_connection_ok = bool(isinstance(conn_connected, int) and conn_connected > 0)
            state_store.append_event(
                {
                    "kind": "cycle",
                    "health_status": health.get("status", "unknown"),
                    "nt_connection_ok": nt_connection_ok,
                    "nt_connections_total": conn_total,
                    "nt_connections_connected": conn_connected,
                    "result": result,
                }
            )
            alert_suffix = ""
            if result.get("alert_skipped"):
                alert_suffix = " alert_skipped=True"
            elif "alert_sent" in result:
                alert_suffix = f" alert_sent={result.get('alert_sent')}"
                if result.get("alert_sent") is False:
                    alert_error = str(result.get("alert_error", "") or "")
                    if len(alert_error) > 160:
                        alert_error = alert_error[:157] + "..."
                    if alert_error:
                        alert_suffix += f" alert_error={alert_error}"
            print(
                f"[{_now()}] health={health.get('status','unknown')} "
                f"nt_connection_ok={nt_connection_ok} nt_connections={conn_connected}/{conn_total} "
                f"action={result.get('action')} reason={result.get('reason')}{alert_suffix}"
            )
            # Publish state so Telegram /health handler has fresh data to read.
            telegram_state.publish(health, runtime_snapshot)
            if max_cycles > 0 and cycle_num >= max_cycles:
                print(f"[{_now()}] max_cycles reached ({max_cycles}); exiting watchdog loop.")
                break
            sleep_sec = int(result.get("sleep_override_sec") or config.poll_interval_sec)
            if sleep_sec != config.poll_interval_sec:
                print(f"[{_now()}] backoff sleep {sleep_sec}s (override)")
            time.sleep(sleep_sec)
    finally:
        try:
            telegram_bot.stop()
        except Exception:
            pass
        single_lock.release()


def main() -> None:
    parser = argparse.ArgumentParser(description="NinjaTrader self-healing watchdog")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to watchdog YAML config file.",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="Run finite cycles for smoke tests (0 = infinite).",
    )
    args = parser.parse_args()
    config = load_config(args.config)
    run_watchdog(config, max_cycles=max(0, args.max_cycles))


if __name__ == "__main__":
    main()

