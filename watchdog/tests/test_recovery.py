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
        return {"method": "uia_toggle", "toggled": 0, "checkbox_count": 0, "error": ""}


class FakeProcessManager:
    def __init__(self, restart_ok: bool = True, redirect_ok: bool = False) -> None:
        self.restart_ok = restart_ok
        self.restart_calls = 0
        self.redirect_ok = redirect_ok
        self.redirect_calls = 0

    def restart(self, startup_grace_sec: int) -> bool:
        self.restart_calls += 1
        return self.restart_ok

    def try_redirect_session_to_console(self, timeout_sec: int = 10) -> bool:
        self.redirect_calls += 1
        return self.redirect_ok


class FakeNotifier:
    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def notify_event(self, event_type: str, incident_id: str, details: Dict[str, Any]) -> bool:
        self.events.append(
            {"event_type": event_type, "incident_id": incident_id, "details": details}
        )
        return True


class RecoveryTests(unittest.TestCase):
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

            manager = RecoveryManager(
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

            manager = RecoveryManager(
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

            manager = RecoveryManager(
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

            manager = RecoveryManager(
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

            manager = RecoveryManager(
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

    def test_no_connections_detected_respects_recovery_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = self._build_config(td)
            cfg.no_connections_recovery_cooldown_sec = 3600
            state = StateStore(cfg)
            bridge = FakeBridge([{"success": True}])
            process = FakeProcessManager(restart_ok=True)
            notifier = FakeNotifier()

            manager = RecoveryManager(
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

            manager = RecoveryManager(
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

            manager = RecoveryManager(
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


if __name__ == "__main__":
    unittest.main()

