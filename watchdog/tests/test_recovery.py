from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from watchdog.config import WatchdogConfig
from watchdog.recovery import RecoveryManager
from watchdog.state_store import StateStore


class FakeBridge:
    def __init__(self, reconnect_results: List[Dict[str, Any]]) -> None:
        self.reconnect_results = reconnect_results
        self.calls = 0
        self.last_connection_names: List[str] = []

    def recover_reconnect(self, flatten_first: bool, connection_names: List[str] | None = None) -> Dict[str, Any]:
        idx = self.calls
        self.calls += 1
        self.last_connection_names = list(connection_names or [])
        if idx >= len(self.reconnect_results):
            return {"success": False, "error": "no more fake responses"}
        return self.reconnect_results[idx]

    def enable_all_strategies(self, timeout_sec: int = 15) -> Dict[str, Any]:
        self.enable_strategies_calls = getattr(self, "enable_strategies_calls", 0) + 1
        return {"method": "uia_keypress", "toggled": 1, "checkbox_count": 1, "error": ""}

    def disable_all_strategies(self, timeout_sec: int = 15) -> Dict[str, Any]:
        self.disable_strategies_calls = getattr(self, "disable_strategies_calls", 0) + 1
        return {"method": "reflection_setstate", "toggled": 1, "count": 1, "error": ""}


    def safe_health(self) -> Dict[str, Any]:
        self.safe_health_calls = getattr(self, "safe_health_calls", 0) + 1
        return {"status": "ok"}


class FakeProcessManager:
    def __init__(
        self,
        restart_ok: bool = True,
        redirect_ok: bool = False,
        manual_ok: bool = True,
    ) -> None:
        self.restart_ok = restart_ok
        self.restart_calls = 0
        self.redirect_ok = redirect_ok
        self.redirect_calls = 0
        self.manual_ok = manual_ok
        self.stop_calls = 0
        self.start_calls = 0

    def restart(self, startup_grace_sec: int) -> bool:
        self.restart_calls += 1
        return self.restart_ok

    def stop(self, timeout_sec: int = 30) -> bool:
        self.stop_calls += 1
        return self.manual_ok

    def start(self) -> bool:
        self.start_calls += 1
        return self.manual_ok

    def is_running(self) -> bool:
        return self.manual_ok

    def try_redirect_session_to_console(self, timeout_sec: int = 10) -> bool:
        self.redirect_calls += 1
        return self.redirect_ok


class FakeNotifier:
    def __init__(self, send_ok: bool = True) -> None:
        self.events: List[Dict[str, Any]] = []
        self.send_ok = send_ok
        self.last_error = ""

    def notify_event(self, event_type: str, incident_id: str, details: Dict[str, Any]) -> bool:
        self.events.append(
            {"event_type": event_type, "incident_id": incident_id, "details": details}
        )
        if not self.send_ok:
            self.last_error = "simulated telegram failure"
            return False
        self.last_error = ""
        return True


class RecoveryTests(unittest.TestCase):
    def _make_manager(self, **kwargs: Any) -> RecoveryManager:
        manager = RecoveryManager(**kwargs)
        manager.post_reconnect_delay_sec = 0
        return manager

    def _build_config(self, temp_dir: str) -> WatchdogConfig:
        return WatchdogConfig(
            reconnect_attempt_limit=1,
            restart_cooldown_sec=1,
            max_restarts_per_hour=5,
            no_connections_recovery_cooldown_sec=0,
            snapshot_path=str(Path(temp_dir) / "state" / "snapshot.json"),
            events_log_path=str(Path(temp_dir) / "logs" / "events.jsonl"),
            telegram_enabled=False,
        )

    def test_reconnect_success(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = self._build_config(td)
            state = StateStore(cfg)
            bridge = FakeBridge([{"success": True, "action": "reconnect"}])
            process = FakeProcessManager(restart_ok=True)
            notifier = FakeNotifier()

            manager = self._make_manager(
                config=cfg,
                bridge=bridge,  # type: ignore[arg-type]
                process_manager=process,  # type: ignore[arg-type]
                state_store=state,
                notifier=notifier,  # type: ignore[arg-type]
            )
            result = manager.handle_cycle(
                health={"status": "degraded", "reasons": ["connection_unstable"]},
                runtime_snapshot={"positions": []},
            )

            self.assertEqual(result["action"], "reconnect")
            self.assertEqual(process.restart_calls, 0)
            self.assertEqual(bridge.calls, 1)

    def test_stuck_triggers_rdp_redirect_before_reconnect(self) -> None:
        """When /healthz reports main_thread_unresponsive, watchdog should try
        to redirect the disconnected RDP session to console BEFORE calling
        /recover/reconnect. Prevents unnecessary broker-reconnect churn when
        the actual cause is a hung WPF renderer."""
        with tempfile.TemporaryDirectory() as td:
            cfg = self._build_config(td)
            state = StateStore(cfg)
            bridge = FakeBridge([])
            process = FakeProcessManager(restart_ok=True, redirect_ok=True)
            notifier = FakeNotifier()

            manager = self._make_manager(
                config=cfg,
                bridge=bridge,  # type: ignore[arg-type]
                process_manager=process,  # type: ignore[arg-type]
                state_store=state,
                notifier=notifier,  # type: ignore[arg-type]
            )
            result = manager.handle_cycle(
                health={"status": "stuck", "reasons": ["main_thread_unresponsive"]},
                runtime_snapshot={"positions": []},
            )

            self.assertEqual(process.redirect_calls, 1)
            self.assertEqual(bridge.calls, 0, "reconnect should be skipped when redirect succeeds")
            self.assertEqual(process.restart_calls, 0)
            self.assertEqual(result["action"], "rdp_redirect")

    def test_reconnect_passes_configured_connection_names(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = self._build_config(td)
            cfg.connection_names = ["My NinjaTrader", "Simulated Data Feed"]
            state = StateStore(cfg)
            bridge = FakeBridge([{"success": True, "action": "reconnect"}])
            process = FakeProcessManager(restart_ok=True)
            notifier = FakeNotifier()

            manager = self._make_manager(
                config=cfg,
                bridge=bridge,  # type: ignore[arg-type]
                process_manager=process,  # type: ignore[arg-type]
                state_store=state,
                notifier=notifier,  # type: ignore[arg-type]
            )
            manager.handle_cycle(
                health={"status": "degraded", "reasons": ["connection_unstable"]},
                runtime_snapshot={"positions": []},
            )

            self.assertEqual(bridge.last_connection_names, ["My NinjaTrader", "Simulated Data Feed"])

    def test_restart_after_reconnect_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = self._build_config(td)
            state = StateStore(cfg)
            bridge = FakeBridge([{"success": False, "error": "bridge timeout"}])
            process = FakeProcessManager(restart_ok=True)
            notifier = FakeNotifier()

            manager = self._make_manager(
                config=cfg,
                bridge=bridge,  # type: ignore[arg-type]
                process_manager=process,  # type: ignore[arg-type]
                state_store=state,
                notifier=notifier,  # type: ignore[arg-type]
            )
            result = manager.handle_cycle(
                health={"status": "stuck", "reasons": ["main_thread_unresponsive"]},
                runtime_snapshot={"positions": []},
            )

            self.assertEqual(result["action"], "restart_nt")
            self.assertEqual(process.restart_calls, 1)

    def test_no_connections_detected_attempts_reconnect(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = self._build_config(td)
            state = StateStore(cfg)
            bridge = FakeBridge([{"success": True}])
            process = FakeProcessManager(restart_ok=True)
            notifier = FakeNotifier()

            manager = self._make_manager(
                config=cfg,
                bridge=bridge,  # type: ignore[arg-type]
                process_manager=process,  # type: ignore[arg-type]
                state_store=state,
                notifier=notifier,  # type: ignore[arg-type]
            )
            result = manager.handle_cycle(
                health={"status": "degraded", "reasons": ["no_connections_detected"]},
                runtime_snapshot={"positions": []},
            )

            self.assertEqual(result["action"], "reconnect")
            self.assertEqual(bridge.calls, 1)
            self.assertEqual(process.restart_calls, 0)
            self.assertEqual(getattr(bridge, "enable_strategies_calls", 0), 1)
            self.assertEqual(result.get("strategies_toggled"), 1)

    def test_no_connections_detected_respects_recovery_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = self._build_config(td)
            cfg.no_connections_recovery_cooldown_sec = 3600
            state = StateStore(cfg)
            bridge = FakeBridge([{"success": True}])
            process = FakeProcessManager(restart_ok=True)
            notifier = FakeNotifier()

            manager = self._make_manager(
                config=cfg,
                bridge=bridge,  # type: ignore[arg-type]
                process_manager=process,  # type: ignore[arg-type]
                state_store=state,
                notifier=notifier,  # type: ignore[arg-type]
            )
            first = manager.handle_cycle(
                health={"status": "degraded", "reasons": ["no_connections_detected"]},
                runtime_snapshot={"positions": []},
            )
            second = manager.handle_cycle(
                health={"status": "degraded", "reasons": ["no_connections_detected"]},
                runtime_snapshot={"positions": []},
            )

            self.assertEqual(first["action"], "reconnect")
            self.assertEqual(second["action"], "notify_only")
            self.assertEqual(second["reason"], "no_connections_detected_cooldown")
            self.assertEqual(bridge.calls, 1)

    def test_no_connections_failed_reconnect_does_not_restart(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = self._build_config(td)
            state = StateStore(cfg)
            bridge = FakeBridge([{"success": False, "error": "bridge timeout"}])
            process = FakeProcessManager(restart_ok=True)
            notifier = FakeNotifier()

            manager = self._make_manager(
                config=cfg,
                bridge=bridge,  # type: ignore[arg-type]
                process_manager=process,  # type: ignore[arg-type]
                state_store=state,
                notifier=notifier,  # type: ignore[arg-type]
            )
            result = manager.handle_cycle(
                health={"status": "degraded", "reasons": ["no_connections_detected"]},
                runtime_snapshot={"positions": []},
            )

            self.assertEqual(result["action"], "notify_only")
            self.assertEqual(result["reason"], "no_connections_reconnect_failed")
            self.assertEqual(process.restart_calls, 0)

    def test_snapshot_restore_manual_required(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = self._build_config(td)
            state = StateStore(cfg)
            bridge = FakeBridge([{"success": True}])
            process = FakeProcessManager(restart_ok=True)
            notifier = FakeNotifier()

            state.save_snapshot(
                {
                    "strategy_runtime": {
                        "strategies": [
                            {
                                "account": "Sim101",
                                "name": "TrendA",
                                "instrument": "ES 06-26",
                                "template": "",
                                "is_enabled": True,
                            }
                        ]
                    }
                }
            )
            runtime = state.load_runtime_state()
            runtime["awaiting_restore"] = True
            state.save_runtime_state(runtime)

            manager = self._make_manager(
                config=cfg,
                bridge=bridge,  # type: ignore[arg-type]
                process_manager=process,  # type: ignore[arg-type]
                state_store=state,
                notifier=notifier,  # type: ignore[arg-type]
            )
            result = manager.handle_cycle(
                health={"status": "ok", "reasons": []},
                runtime_snapshot={"strategy_runtime": {"strategies": []}, "positions": []},
            )

            self.assertIn("restore", result)
            self.assertFalse(result["restore"]["restored"])

    def test_cooldown_not_started_when_send_fails(self) -> None:
        """Regression: a failed Telegram send must NOT advance the cooldown
        timestamp — otherwise retries get skipped while the incident is still
        undelivered."""
        with tempfile.TemporaryDirectory() as td:
            cfg = self._build_config(td)
            cfg.notification_cooldown_sec = 3600
            state = StateStore(cfg)
            bridge = FakeBridge([])
            process = FakeProcessManager(restart_ok=True)
            notifier = FakeNotifier(send_ok=False)

            manager = self._make_manager(
                config=cfg,
                bridge=bridge,  # type: ignore[arg-type]
                process_manager=process,  # type: ignore[arg-type]
                state_store=state,
                notifier=notifier,  # type: ignore[arg-type]
            )
            r1 = manager._notify("process_started", {"status": "recovering", "reason": "process_not_running"})
            r2 = manager._notify("process_started", {"status": "recovering", "reason": "process_not_running"})

            self.assertFalse(r1["alert_sent"])
            self.assertFalse(r2["alert_sent"])
            # Both attempts actually called the notifier — neither was skipped.
            self.assertEqual(len(notifier.events), 2)
            self.assertNotIn("process_not_running", manager.runtime_state.get("notification_state", {}))

    def test_successful_send_starts_cooldown(self) -> None:
        """After a successful send, the next same-key call within the window
        must be skipped."""
        with tempfile.TemporaryDirectory() as td:
            cfg = self._build_config(td)
            cfg.notification_cooldown_sec = 3600
            state = StateStore(cfg)
            bridge = FakeBridge([])
            process = FakeProcessManager(restart_ok=True)
            notifier = FakeNotifier(send_ok=True)

            manager = self._make_manager(
                config=cfg,
                bridge=bridge,  # type: ignore[arg-type]
                process_manager=process,  # type: ignore[arg-type]
                state_store=state,
                notifier=notifier,  # type: ignore[arg-type]
            )
            r1 = manager._notify("process_started", {"status": "recovering", "reason": "boot"})
            r2 = manager._notify("process_started", {"status": "recovering", "reason": "boot"})

            self.assertTrue(r1["alert_sent"])
            self.assertTrue(r2.get("alert_skipped"))
            self.assertEqual(len(notifier.events), 1)

    def test_reconnect_retries_share_cooldown_bucket(self) -> None:
        """Reconnect retries pass attempt_1_failed, attempt_2_failed… as
        reason. They must share one cooldown bucket via explicit dedupe_key,
        so repeated retries don't spam Telegram."""
        with tempfile.TemporaryDirectory() as td:
            cfg = self._build_config(td)
            cfg.notification_cooldown_sec = 3600
            cfg.reconnect_attempt_limit = 10
            state = StateStore(cfg)
            bridge = FakeBridge([
                {"success": False, "error": "e1"},
                {"success": False, "error": "e2"},
                {"success": False, "error": "e3"},
            ])
            process = FakeProcessManager(restart_ok=True)
            notifier = FakeNotifier(send_ok=True)

            manager = self._make_manager(
                config=cfg,
                bridge=bridge,  # type: ignore[arg-type]
                process_manager=process,  # type: ignore[arg-type]
                state_store=state,
                notifier=notifier,  # type: ignore[arg-type]
            )
            for _ in range(3):
                manager.handle_cycle(
                    health={"status": "degraded", "reasons": ["connection_unstable"]},
                    runtime_snapshot={"positions": []},
                )

            retry_sends = [e for e in notifier.events if e["event_type"] == "reconnect_retrying"]
            # Cooldown is 1h so only the first retrying notification should
            # have gone out; the rest must dedupe under 'reconnect_retrying'.
            self.assertEqual(len(retry_sends), 1)

    def test_process_started_notification_failure_is_logged(self) -> None:
        """Regression: when Telegram send fails for the process_started
        branch, the event log must record sent=False with the error so
        operators can see it instead of silently dropping."""
        with tempfile.TemporaryDirectory() as td:
            cfg = self._build_config(td)
            state = StateStore(cfg)
            bridge = FakeBridge([])
            process = FakeProcessManager(restart_ok=True)
            notifier = FakeNotifier(send_ok=False)

            manager = self._make_manager(
                config=cfg,
                bridge=bridge,  # type: ignore[arg-type]
                process_manager=process,  # type: ignore[arg-type]
                state_store=state,
                notifier=notifier,  # type: ignore[arg-type]
            )
            manager._notify(
                "process_started",
                {"status": "recovering", "action": "start_nt", "reason": "process_not_running"},
                incident_id="abc123",
            )

            log_lines = Path(cfg.events_log_path).read_text(encoding="utf-8").strip().splitlines()
            import json as _json
            notif_events = [
                _json.loads(l) for l in log_lines if _json.loads(l).get("kind") == "notification"
            ]
            self.assertEqual(len(notif_events), 1)
            self.assertEqual(notif_events[0]["sent"], False)
            self.assertEqual(notif_events[0]["error"], "simulated telegram failure")


class ManualRestartTests(unittest.TestCase):
    def _build_config(self, temp_dir: str) -> WatchdogConfig:
        return WatchdogConfig(
            reconnect_attempt_limit=1,
            restart_cooldown_sec=1,
            max_restarts_per_hour=2,
            no_connections_recovery_cooldown_sec=0,
            startup_grace_sec=0,
            snapshot_path=str(Path(temp_dir) / "state" / "snapshot.json"),
            events_log_path=str(Path(temp_dir) / "logs" / "events.jsonl"),
            telegram_enabled=False,
        )

    def test_manual_restart_bypasses_breaker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = self._build_config(td)
            state = StateStore(cfg)
            bridge = FakeBridge([])
            process = FakeProcessManager(manual_ok=True)
            notifier = FakeNotifier()

            # Pre-seed runtime state to exceed breaker threshold.
            runtime = state.load_runtime_state()
            now = "2099-01-01T00:00:00Z"
            runtime["restarts"] = [now, now, now]
            state.save_runtime_state(runtime)

            manager = RecoveryManager(
                config=cfg,
                bridge=bridge,  # type: ignore[arg-type]
                process_manager=process,  # type: ignore[arg-type]
                state_store=state,
                notifier=notifier,  # type: ignore[arg-type]
            )
            manager.post_reconnect_delay_sec = 0

            result = manager.manual_restart(bridge_wait_sec=0)

            self.assertTrue(result["ok"])
            self.assertEqual(result["stop_mode"], "forced")
            self.assertEqual(result["strategies_toggled"], 1)
            self.assertEqual(process.stop_calls, 1)
            self.assertEqual(process.start_calls, 1)
            self.assertEqual(process.restart_calls, 0)
            self.assertEqual(getattr(bridge, "disable_strategies_calls", 0), 1)
            self.assertEqual(getattr(bridge, "enable_strategies_calls", 0), 1)

            restart_events = [
                e for e in notifier.events if e["event_type"] == "restart_success"
            ]
            self.assertEqual(len(restart_events), 1)
            self.assertEqual(restart_events[0]["details"].get("reason"), "manual_telegram")

    def test_manual_restart_fails_when_process_does_not_come_up(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = self._build_config(td)
            state = StateStore(cfg)
            bridge = FakeBridge([])
            process = FakeProcessManager(manual_ok=False)
            notifier = FakeNotifier()

            manager = RecoveryManager(
                config=cfg,
                bridge=bridge,  # type: ignore[arg-type]
                process_manager=process,  # type: ignore[arg-type]
                state_store=state,
                notifier=notifier,  # type: ignore[arg-type]
            )
            manager.post_reconnect_delay_sec = 0

            result = manager.manual_restart(bridge_wait_sec=0)

            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "process_restart_failed")
            self.assertEqual(getattr(bridge, "enable_strategies_calls", 0), 0)


if __name__ == "__main__":
    unittest.main()

