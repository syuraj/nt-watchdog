from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

from .bridge_client import BridgeClient
from .config import WatchdogConfig
from .nt_process import NTProcessManager
from .state_store import StateStore
from .strategy_ui_restore import StrategyUiRestorer
from .telegram_notifier import TelegramNotifier


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class RecoveryManager:
    def __init__(
        self,
        config: WatchdogConfig,
        bridge: BridgeClient,
        process_manager: NTProcessManager,
        state_store: StateStore,
        notifier: Optional[TelegramNotifier] = None,
        restorer: Optional[StrategyUiRestorer] = None,
    ) -> None:
        self.config = config
        self.bridge = bridge
        self.process_manager = process_manager
        self.state_store = state_store
        self.notifier = notifier or TelegramNotifier(config)
        self.restorer = restorer or StrategyUiRestorer()
        self.runtime_state = self.state_store.load_runtime_state()
        self.post_reconnect_delay_sec = 15
        self._manual_lock = threading.Lock()

    def _persist_runtime(self) -> None:
        self.state_store.save_runtime_state(self.runtime_state)

    def _get_incident_id(self) -> str:
        incident_id = self.runtime_state.get("last_incident_id", "")
        if incident_id:
            return incident_id
        incident_id = uuid4().hex[:10]
        self.runtime_state["last_incident_id"] = incident_id
        self._persist_runtime()
        return incident_id

    def _clear_incident(self) -> str:
        old = self.runtime_state.get("last_incident_id", "")
        self.runtime_state["last_incident_id"] = ""
        self.runtime_state["reconnect_failures"] = 0
        self._persist_runtime()
        return old

    def _has_open_positions(self, runtime_snapshot: Dict[str, Any]) -> bool:
        positions = runtime_snapshot.get("positions", [])
        if not isinstance(positions, list):
            return False
        for pos in positions:
            qty = pos.get("quantity", 0)
            side = str(pos.get("side", "Flat"))
            if qty and side.lower() != "flat":
                return True
        return False

    def _can_restart_now(self) -> bool:
        restarts = self.runtime_state.get("restarts", [])
        if not isinstance(restarts, list):
            restarts = []
        restarts = self.state_store.prune_restart_history(restarts)
        self.runtime_state["restarts"] = restarts
        if len(restarts) >= self.config.max_restarts_per_hour:
            return False
        if restarts:
            last = datetime.fromisoformat(restarts[-1].replace("Z", "+00:00"))
            elapsed = (datetime.now(timezone.utc) - last).total_seconds()
            if elapsed < self.config.restart_cooldown_sec:
                return False
        return True

    def _mark_restart(self, awaiting_restore: bool = True) -> None:
        restarts = self.runtime_state.get("restarts", [])
        if not isinstance(restarts, list):
            restarts = []
        restarts = self.state_store.prune_restart_history(restarts)
        restarts.append(_utc_now())
        self.runtime_state["restarts"] = restarts
        self.runtime_state["awaiting_restore"] = awaiting_restore
        self._persist_runtime()

    def _notify(
        self,
        event_type: str,
        details: Dict[str, Any],
        incident_id: str = "",
        dedupe_key: str = "",
    ) -> Dict[str, Any]:
        resolved_incident_id = incident_id or self._get_incident_id()
        # Callers that want retries to share a cooldown bucket pass an explicit
        # dedupe_key; otherwise fall back to reason/event_type.
        resolved_dedupe = dedupe_key or str(details.get("reason", "") or event_type)
        if self._is_in_cooldown(resolved_dedupe):
            event = {
                "kind": "notification",
                "event_type": event_type,
                "incident_id": resolved_incident_id,
                "sent": None,
                "skipped": True,
                "reason": "cooldown",
            }
            self.state_store.append_event(event)
            return {"alert_sent": None, "alert_error": "", "alert_skipped": True}

        sent = self.notifier.notify_event(event_type, resolved_incident_id, details)
        err = str(getattr(self.notifier, "last_error", "") or "")
        # Only start the cooldown window on a successful send — otherwise a
        # transient Telegram outage would suppress every retry until the
        # cooldown expires, masking the incident entirely.
        if sent:
            self._record_notification_sent(resolved_dedupe)
        event = {
            "kind": "notification",
            "event_type": event_type,
            "incident_id": resolved_incident_id,
            "sent": bool(sent),
        }
        if not sent and err:
            event["error"] = err
        self.state_store.append_event(event)
        return {"alert_sent": bool(sent), "alert_error": err if not sent else ""}

    def _is_in_cooldown(self, dedupe_key: str) -> bool:
        cooldown = int(getattr(self.config, "notification_cooldown_sec", 0) or 0)
        if cooldown <= 0:
            return False
        state = self.runtime_state.get("notification_state", {})
        if not isinstance(state, dict):
            return False
        raw = state.get(dedupe_key)
        if not isinstance(raw, str):
            return False
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return False
        return (datetime.now(timezone.utc) - ts).total_seconds() < cooldown

    def _record_notification_sent(self, dedupe_key: str) -> None:
        state = self.runtime_state.get("notification_state", {})
        if not isinstance(state, dict):
            state = {}
        state[dedupe_key] = _utc_now()
        self.runtime_state["notification_state"] = state
        self._persist_runtime()

    def _store_last_good_snapshot(self, runtime_snapshot: Dict[str, Any], health: Dict[str, Any]) -> None:
        if not runtime_snapshot or runtime_snapshot.get("error"):
            return
        payload = {
            "saved_utc": _utc_now(),
            "health": health,
            **runtime_snapshot,
        }
        self.state_store.save_snapshot(payload)

    def _attempt_snapshot_restore(self, current_runtime: Dict[str, Any]) -> Dict[str, Any]:
        snapshot = self.state_store.load_snapshot()
        if not snapshot:
            self.runtime_state["awaiting_restore"] = False
            self._persist_runtime()
            return {"restored": False, "reason": "missing_snapshot"}

        ok, message = self.restorer.restore(snapshot=snapshot, current_runtime=current_runtime)
        self.runtime_state["awaiting_restore"] = False
        self._persist_runtime()
        if ok:
            self.state_store.append_event({"kind": "restore", "status": "success", "message": message})
            return {"restored": True, "reason": message}
        self.state_store.append_event({"kind": "restore", "status": "manual", "message": message})
        self._notify("restore_manual_required", {"status": "degraded", "action": "manual_restore", "reason": message})
        return {"restored": False, "reason": message}

    def _attempt_reconnect(self, runtime_snapshot: Dict[str, Any], extra_event: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Run a single reconnect attempt and log the outcome as a reconnect_attempt event.

        Returns a dict with keys: success, open_positions, bridge_unreachable, details.
        `extra_event` is merged into the logged event (e.g. to record a reason tag).
        """
        open_positions = self._has_open_positions(runtime_snapshot)
        reconnect_result = self.bridge.recover_reconnect(
            flatten_first=open_positions,
            connection_names=self.config.connection_names,
        )
        success = bool(reconnect_result.get("success"))
        err_text = str(reconnect_result.get("error", "") or "").lower()
        # Bridge transport failure (NT dead, bridge not listening) is not a reconnect-logic
        # failure — callers should skip escalation and wait for the bootstrap path.
        bridge_unreachable = (not success) and any(
            marker in err_text
            for marker in ("urlopen error", "winerror 10061", "connection refused", "actively refused")
        )
        event = {
            "kind": "reconnect_attempt",
            "success": success,
            "open_positions": open_positions,
            "bridge_unreachable": bridge_unreachable,
            "details": reconnect_result,
        }
        if extra_event:
            event.update(extra_event)
        self.state_store.append_event(event)
        return {
            "success": success,
            "open_positions": open_positions,
            "bridge_unreachable": bridge_unreachable,
            "details": reconnect_result,
        }

    def _degraded_notify(
        self,
        *,
        event_type: str,
        status: str,
        reason: str,
        incident_id: str,
        action: str = "notify_only",
        state: str = "degraded",
        log_event: bool = False,
    ) -> Dict[str, Any]:
        """Build a degraded/notify-only result dict and fire a notification.

        Consolidates the three near-identical result-building blocks used by the
        no_connections branches and the restart-circuit-breaker branch.
        """
        result = {
            "state": state,
            "action": action,
            "reason": reason,
            "incident_id": incident_id,
        }
        if log_event:
            self.state_store.append_event(result)
        notify_meta = self._notify(
            event_type,
            {"status": status, "action": action, "reason": reason},
        )
        result.update(notify_meta)
        return result

    def _should_attempt_no_connections_recovery(self) -> bool:
        cooldown = int(getattr(self.config, "no_connections_recovery_cooldown_sec", 0) or 0)
        if cooldown <= 0:
            return True
        now = datetime.now(timezone.utc)
        key = "last_no_connections_recovery_utc"
        raw = self.runtime_state.get(key, "")
        if isinstance(raw, str) and raw:
            try:
                ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if (now - ts).total_seconds() < cooldown:
                    return False
            except Exception:
                pass
        self.runtime_state[key] = _utc_now()
        self._persist_runtime()
        return True

    def handle_cycle(self, health: Dict[str, Any], runtime_snapshot: Dict[str, Any]) -> Dict[str, Any]:
        self.runtime_state = self.state_store.load_runtime_state()

        status = str(health.get("status", "down")).lower()
        reasons = health.get("reasons", [])
        reason_str = ",".join(reasons) if isinstance(reasons, list) else str(reasons)

        if status == "ok":
            prior_incident = self.runtime_state.get("last_incident_id", "")
            self.runtime_state["reconnect_failures"] = 0
            restore_info = {}
            resolved_alert_meta = {}
            if self.runtime_state.get("awaiting_restore"):
                restore_info = self._attempt_snapshot_restore(runtime_snapshot)
                # Keep prior snapshot when restore is still manual-required.
                if restore_info.get("restored"):
                    self._store_last_good_snapshot(runtime_snapshot, health)
            else:
                self._store_last_good_snapshot(runtime_snapshot, health)
            self._persist_runtime()
            if prior_incident:
                # Log the resolution as an event but skip the Telegram send — a
                # prior reconnect_success / restart_success already notified that
                # recovery worked; a second "resolved" message is noise.
                self.state_store.append_event(
                    {
                        "kind": "notification",
                        "event_type": "incident_resolved",
                        "incident_id": prior_incident,
                        "sent": None,
                        "skipped": True,
                        "reason": "suppressed_after_success",
                    }
                )
                self._clear_incident()
            result = {"state": "healthy", "action": "none", "reason": "ok"}
            if restore_info:
                result["restore"] = restore_info
            if resolved_alert_meta:
                result.update(resolved_alert_meta)
            return result

        incident_id = self._get_incident_id()

        # RDP-disconnect-induced UI freeze: a disconnected RDP session detaches the
        # virtual display driver, which suspends WPF Direct3D rendering. Main thread
        # hangs while market data still flows. Before the normal reconnect/restart
        # escalation, try redirecting any disconnected RDP session to the console —
        # that re-binds the GPU and rendering resumes without touching NT.
        ui_stuck = status == "stuck" and isinstance(reasons, list) and "main_thread_unresponsive" in reasons
        if ui_stuck and hasattr(self.process_manager, "try_redirect_session_to_console"):
            redirected = self.process_manager.try_redirect_session_to_console()
            self.state_store.append_event({"kind": "rdp_redirect", "success": redirected, "incident_id": incident_id})
            if redirected:
                return self._degraded_notify(
                    event_type="rdp_redirect_attempted",
                    status=status,
                    reason="ui_thread_unresponsive_redirected",
                    incident_id=incident_id,
                    action="rdp_redirect",
                )
            # Fall through to the normal reconnect/restart flow if redirect failed.

        no_connections = isinstance(reasons, list) and "no_connections_detected" in reasons
        if no_connections and not self._should_attempt_no_connections_recovery():
            return self._degraded_notify(
                event_type="no_connections_detected",
                status=status,
                reason="no_connections_detected_cooldown",
                incident_id=incident_id,
                log_event=True,
            )

        # Safety policy: if there are no NT connections, attempt reconnect only.
        # Do not escalate to process restart from this reason alone.
        if no_connections:
            attempt = self._attempt_reconnect(runtime_snapshot, extra_event={"reason": "no_connections_detected"})
            self.runtime_state["reconnect_failures"] = 0
            self._persist_runtime()
            if attempt["success"]:
                time.sleep(self.post_reconnect_delay_sec)
                strat_result = self.bridge.enable_all_strategies()
                self.state_store.append_event(
                    {
                        "kind": "strategies_enable",
                        "toggled": strat_result.get("toggled", 0),
                        "checkbox_count": strat_result.get("checkbox_count", 0),
                        "details": strat_result,
                    }
                )
                notify_meta = self._notify(
                    "reconnect_success",
                    {
                        "status": status,
                        "action": "reconnect",
                        "reason": "no_connections_reconnect_success",
                        "strategies_toggled": strat_result.get("toggled", 0),
                    },
                )
                result = {
                    "state": "recovering",
                    "action": "reconnect",
                    "reason": "no_connections_reconnect_success",
                    "incident_id": incident_id,
                    "strategies_toggled": strat_result.get("toggled", 0),
                }
                result.update(notify_meta)
                return result

            # Keep restart path disabled for no-connections incidents.
            return self._degraded_notify(
                event_type="no_connections_detected",
                status=status,
                reason="no_connections_reconnect_failed",
                incident_id=incident_id,
                log_event=True,
            )

        attempt = self._attempt_reconnect(runtime_snapshot)
        reconnect_ok = attempt["success"]
        if attempt["bridge_unreachable"]:
            # Skip counter increment + escalation; wait for bootstrap path to revive NT.
            result = {
                "state": "degraded",
                "action": "wait_for_bridge",
                "reason": "bridge_unreachable",
                "incident_id": incident_id,
                "sleep_override_sec": self.config.poll_interval_sec,
            }
            return result
        if reconnect_ok:
            self.runtime_state["reconnect_failures"] = 0
            self._persist_runtime()
            # Give NT time for account/broker subscription to finish after the
            # connection reports Connected — strategy activation fails silently
            # when account isn't yet fully bound.
            time.sleep(self.post_reconnect_delay_sec)
            # After a successful reconnect, re-enable any strategies that went offline
            # when the broker dropped. Idempotent — already-enabled rows are skipped.
            strat_result = self.bridge.enable_all_strategies()
            self.state_store.append_event(
                {
                    "kind": "strategies_enable",
                    "toggled": strat_result.get("toggled", 0),
                    "checkbox_count": strat_result.get("checkbox_count", 0),
                    "details": strat_result,
                }
            )
            notify_meta = self._notify(
                "reconnect_success",
                {
                    "status": status,
                    "action": "reconnect",
                    "reason": reason_str or "reconnect_success",
                    "strategies_toggled": strat_result.get("toggled", 0),
                },
            )
            result = {
                "state": "recovering",
                "action": "reconnect",
                "reason": "reconnect_success",
                "incident_id": incident_id,
                "strategies_toggled": strat_result.get("toggled", 0),
            }
            result.update(notify_meta)
            return result

        failures = int(self.runtime_state.get("reconnect_failures", 0)) + 1
        self.runtime_state["reconnect_failures"] = failures
        self._persist_runtime()

        if failures < self.config.reconnect_attempt_limit:
            # Exponential backoff: double the sleep each failure, capped at 8x the poll interval.
            # Base cadence stays poll_interval_sec (60s); after failures=1 sleep extra 60s → total 2m,
            # failures=2 extra 180s → total 4m. Prevents log/alert noise on permanently-bad creds.
            base = self.config.poll_interval_sec
            extra = min(base * (2 ** (failures - 1) - 1), base * 7)
            sleep_override = base + extra
            notify_meta = self._notify(
                "reconnect_retrying",
                {
                    "status": status,
                    "action": "reconnect_retry",
                    "reason": f"attempt_{failures}_failed",
                    "next_retry_sec": sleep_override,
                },
                dedupe_key="reconnect_retrying",
            )
            result = {
                "state": "degraded",
                "action": "reconnect_retry_wait",
                "reason": f"reconnect_attempt_{failures}_failed",
                "incident_id": incident_id,
                "sleep_override_sec": sleep_override,
            }
            result.update(notify_meta)
            return result

        if not self._can_restart_now():
            return self._degraded_notify(
                event_type="restart_blocked",
                status=status,
                reason="restart_circuit_breaker_open",
                incident_id=incident_id,
            )

        restart_reason = f"reconnect_attempt_{failures}_failed_restart"
        restarted = self.process_manager.restart(startup_grace_sec=self.config.startup_grace_sec)
        if restarted:
            self.runtime_state["reconnect_failures"] = 0
            self._mark_restart()
            self.state_store.append_event(
                {
                    "kind": "restart",
                    "status": "success",
                    "incident_id": incident_id,
                    "reason": restart_reason,
                }
            )
            notify_meta = self._notify(
                "restart_success",
                {"status": "recovering", "action": "restart_nt", "reason": restart_reason},
            )
            result = {
                "state": "recovering",
                "action": "restart_nt",
                "reason": restart_reason,
                "incident_id": incident_id,
            }
            result.update(notify_meta)
            return result

        self.state_store.append_event(
            {"kind": "restart", "status": "failed", "incident_id": incident_id, "reason": "process_restart_failed"}
        )
        notify_meta = self._notify(
            "restart_failed",
            {"status": "degraded", "action": "restart_nt", "reason": "process_restart_failed"},
        )
        result = {
            "state": "degraded",
            "action": "restart_failed",
            "reason": "process_restart_failed",
            "incident_id": incident_id,
        }
        result.update(notify_meta)
        return result

    def manual_restart(self, bridge_wait_sec: int = 120) -> Dict[str, Any]:
        """User-initiated restart from Telegram /restart. Bypasses the
        max_restarts_per_hour breaker (manual intent overrides automation
        heuristics) but still records a restart timestamp so the automated
        path sees it afterwards.
        """
        if not self._manual_lock.acquire(blocking=False):
            return {"ok": False, "stop_mode": "", "strategies_toggled": 0, "error": "manual_restart_in_progress"}
        try:
            self.runtime_state = self.state_store.load_runtime_state()
            incident_id = uuid4().hex[:10]
            reason = "manual_telegram"

            # Pre-stop: disable all strategies via reflection. NT8 pops a
            # "N strategies running" modal on WM_CLOSE when any are active, which
            # blocks graceful shutdown. Terminating first lets taskkill proceed
            # without user interaction.
            pre_disable = self.bridge.disable_all_strategies()
            self.state_store.append_event(
                {
                    "kind": "strategies_disable",
                    "toggled": pre_disable.get("toggled", 0),
                    "count": pre_disable.get("count", 0),
                    "details": pre_disable,
                    "trigger": reason,
                }
            )

            # Force kill: WM_CLOSE triggers NT's "Save workspace?" modal which
            # blocks headless shutdown. An earlier reflection-based workspace
            # save (SaveWorkspaceAs) corrupted the file, so we skip the
            # graceful path entirely. Strategies already terminated above.
            if not self.process_manager.stop():
                ok = False
            elif not self.process_manager.start():
                ok = False
            else:
                ok = True
            stop_mode = "forced" if ok else "failed"
            if not ok:
                self.state_store.append_event(
                    {"kind": "restart", "status": "failed", "incident_id": incident_id, "reason": reason, "stop_mode": stop_mode}
                )
                self._notify("restart_failed", {"status": "degraded", "action": "manual_restart", "reason": reason, "stop_mode": stop_mode})
                return {"ok": False, "stop_mode": stop_mode, "strategies_toggled": 0, "error": "process_restart_failed"}

            self.runtime_state["reconnect_failures"] = 0
            # Manual restart does its own enable_all below, so skip the
            # awaiting_restore flag — otherwise the next watchdog cycle runs
            # _attempt_snapshot_restore and may fire a duplicate Telegram
            # telling the user "Manual enable required".
            self._mark_restart(awaiting_restore=False)

            # Skip the blind startup_grace sleep — poll bridge directly. NT is
            # ready when /health returns ok; polling 2s beats blind 90s wait.
            deadline = time.time() + max(0, bridge_wait_sec)
            bridge_up = False
            while time.time() < deadline:
                health = self.bridge.safe_health()
                if str(health.get("status", "")).lower() == "ok":
                    bridge_up = True
                    break
                time.sleep(2)

            if bridge_up:
                # Short settle so broker account subscription finishes before
                # enable_all. Empirically 3s is enough; was 15s.
                time.sleep(3)
                # Dismiss benign startup dialogs ("window outside viewable
                # range", license prompts) that would block subsequent UIA
                # automation like enable_all.
                dismiss = self.bridge.dismiss_blocking_dialogs()
                if dismiss.get("dismissed", 0):
                    self.state_store.append_event(
                        {
                            "kind": "dialogs_dismissed",
                            "dismissed": dismiss.get("dismissed", 0),
                            "clicked": dismiss.get("clicked", []),
                            "trigger": reason,
                        }
                    )
            # Retry enable_all up to 3 times if the Strategies grid is empty
            # (UIA can't find checkboxes). NT's Control Center populates the
            # grid lazily after boot; a too-early scan returns checkbox_count=0.
            strat_result: Dict[str, Any] = {}
            for attempt in range(3):
                # Dismiss any dialog that popped after boot (esp. "window
                # outside viewable range" — NT may show it late). Always call,
                # cheap no-op when no dialog present.
                self.bridge.dismiss_blocking_dialogs()
                strat_result = self.bridge.enable_all_strategies()
                checkbox_count = int(strat_result.get("checkbox_count", 0) or 0)
                if checkbox_count > 0:
                    break
                if attempt < 2:
                    time.sleep(5)
            toggled = int(strat_result.get("toggled", 0) or 0)
            self.state_store.append_event(
                {
                    "kind": "strategies_enable",
                    "toggled": toggled,
                    "checkbox_count": strat_result.get("checkbox_count", 0),
                    "details": strat_result,
                    "trigger": reason,
                }
            )
            self.state_store.append_event(
                {
                    "kind": "restart",
                    "status": "success",
                    "incident_id": incident_id,
                    "reason": reason,
                    "stop_mode": stop_mode,
                    "bridge_up": bridge_up,
                }
            )
            self._notify(
                "restart_success",
                {"status": "recovering", "action": "manual_restart", "reason": reason, "stop_mode": stop_mode, "strategies_toggled": toggled},
            )
            return {
                "ok": True,
                "stop_mode": stop_mode,
                "strategies_toggled": toggled,
                "bridge_up": bridge_up,
                "error": "",
            }
        finally:
            self._manual_lock.release()

