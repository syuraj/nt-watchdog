from __future__ import annotations

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

    def _mark_restart(self) -> None:
        restarts = self.runtime_state.get("restarts", [])
        if not isinstance(restarts, list):
            restarts = []
        restarts = self.state_store.prune_restart_history(restarts)
        restarts.append(_utc_now())
        self.runtime_state["restarts"] = restarts
        self.runtime_state["awaiting_restore"] = True
        self._persist_runtime()

    def _notify(self, event_type: str, details: Dict[str, Any], incident_id: str = "") -> Dict[str, Any]:
        resolved_incident_id = incident_id or self._get_incident_id()
        dedupe_key = str(details.get("reason", "") or event_type)
        if not self._should_send_notification(dedupe_key):
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

    def _should_send_notification(self, dedupe_key: str) -> bool:
        cooldown = int(getattr(self.config, "notification_cooldown_sec", 0) or 0)
        if cooldown <= 0:
            return True
        state = self.runtime_state.get("notification_state", {})
        if not isinstance(state, dict):
            state = {}
        now = datetime.now(timezone.utc)
        raw = state.get(dedupe_key)
        if isinstance(raw, str):
            try:
                ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if (now - ts).total_seconds() < cooldown:
                    return False
            except Exception:
                pass
        state[dedupe_key] = _utc_now()
        self.runtime_state["notification_state"] = state
        self._persist_runtime()
        return True

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
                resolved_alert_meta = self._notify(
                    "incident_resolved",
                    {"status": "ok", "action": "none", "reason": "health_restored"},
                    incident_id=prior_incident,
                )
                self._clear_incident()
            result = {"state": "healthy", "action": "none", "reason": "ok"}
            if restore_info:
                result["restore"] = restore_info
            if resolved_alert_meta:
                result.update(resolved_alert_meta)
            return result

        incident_id = self._get_incident_id()

        # If NT reports no connection objects, do not spam reconnect/restart loops.
        if isinstance(reasons, list) and "no_connections_detected" in reasons:
            result = {
                "state": "degraded",
                "action": "notify_only",
                "reason": "no_connections_detected",
                "incident_id": incident_id,
            }
            self.state_store.append_event(result)
            notify_meta = self._notify(
                "no_connections_detected",
                {"status": status, "action": "notify_only", "reason": "no_connections_detected"},
            )
            result.update(notify_meta)
            return result

        open_positions = self._has_open_positions(runtime_snapshot)
        reconnect_result = self.bridge.recover_reconnect(flatten_first=open_positions)
        reconnect_ok = bool(reconnect_result.get("success"))
        self.state_store.append_event(
            {
                "kind": "reconnect_attempt",
                "success": reconnect_ok,
                "open_positions": open_positions,
                "details": reconnect_result,
            }
        )
        if reconnect_ok:
            self.runtime_state["reconnect_failures"] = 0
            self._persist_runtime()
            notify_meta = self._notify(
                "reconnect_success",
                {"status": status, "action": "reconnect", "reason": reason_str or "reconnect_success"},
            )
            result = {
                "state": "recovering",
                "action": "reconnect",
                "reason": "reconnect_success",
                "incident_id": incident_id,
            }
            result.update(notify_meta)
            return result

        failures = int(self.runtime_state.get("reconnect_failures", 0)) + 1
        self.runtime_state["reconnect_failures"] = failures
        self._persist_runtime()

        if failures < self.config.reconnect_attempt_limit:
            notify_meta = self._notify(
                "reconnect_retrying",
                {
                    "status": status,
                    "action": "reconnect_retry",
                    "reason": f"attempt_{failures}_failed",
                },
            )
            result = {
                "state": "degraded",
                "action": "reconnect_retry_wait",
                "reason": f"reconnect_attempt_{failures}_failed",
                "incident_id": incident_id,
            }
            result.update(notify_meta)
            return result

        if not self._can_restart_now():
            notify_meta = self._notify(
                "restart_blocked",
                {
                    "status": status,
                    "action": "notify_only",
                    "reason": "restart_circuit_breaker_open",
                },
            )
            result = {
                "state": "degraded",
                "action": "notify_only",
                "reason": "restart_circuit_breaker_open",
                "incident_id": incident_id,
            }
            result.update(notify_meta)
            return result

        restarted = self.process_manager.restart(startup_grace_sec=self.config.startup_grace_sec)
        if restarted:
            self.runtime_state["reconnect_failures"] = 0
            self._mark_restart()
            self.state_store.append_event(
                {
                    "kind": "restart",
                    "status": "success",
                    "incident_id": incident_id,
                    "reason": reason_str or "reconnect_limit_reached",
                }
            )
            notify_meta = self._notify(
                "restart_success",
                {"status": "recovering", "action": "restart_nt", "reason": "restart_completed"},
            )
            result = {
                "state": "recovering",
                "action": "restart_nt",
                "reason": "reconnect_limit_reached",
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

