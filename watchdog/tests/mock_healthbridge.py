from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, List


class MockState:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.recovery_calls = 0


class Handler(BaseHTTPRequestHandler):
    state: MockState

    def _json(self, payload: Dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _runtime_snapshot(self) -> Dict:
        return {
            "generated_utc": "2026-01-01T00:00:00Z",
            "health": self._health(),
            "connections": [{"name": "MockConn", "status": "Connected"}],
            "strategy_runtime": {
                "collection_found": True,
                "active_count": 1,
                "total_count": 1,
                "strategies": [
                    {
                        "name": "MockStrategy",
                        "account": "Sim101",
                        "instrument": "ES 06-26",
                        "template": "",
                        "state": "Running",
                        "is_enabled": True,
                    }
                ],
                "error": "",
            },
            "blocking_windows": [],
            "blocking_windows_count": 0,
            "accounts": [{"name": "Sim101", "connected": True}],
            "positions": [],
        }

    def _health(self) -> Dict:
        if self.state.mode == "degraded":
            return {
                "status": "degraded",
                "service": "HealthBridge",
                "version": "0.4.0",
                "reasons": ["connection_unstable"],
                "connections": {"total": 1, "connected": 0, "unstable": 1},
            }
        return {
            "status": "ok",
            "service": "HealthBridge",
            "version": "0.4.0",
            "reasons": [],
            "connections": {"total": 1, "connected": 1, "unstable": 0},
        }

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._json(self._health())
            return
        if self.path == "/runtime_snapshot":
            self._json(self._runtime_snapshot())
            return
        self._json({"error": "not found", "path": self.path}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path in {"/recover/reconnect", "/recover/flatten_then_reconnect"}:
            self.state.recovery_calls += 1
            self._json(
                {
                    "success": True,
                    "action": "flatten_then_reconnect" if "flatten" in self.path else "reconnect",
                    "recovery_calls": self.state.recovery_calls,
                }
            )
            return
        self._json({"error": "not found", "path": self.path}, status=404)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock HealthBridge server for watchdog smoke tests.")
    parser.add_argument("--port", type=int, default=18999)
    parser.add_argument("--mode", choices=["ok", "degraded"], default="ok")
    args = parser.parse_args()

    Handler.state = MockState(mode=args.mode)
    server = HTTPServer(("127.0.0.1", args.port), Handler)
    print(f"mock healthbridge listening on http://127.0.0.1:{args.port} mode={args.mode}")
    server.serve_forever()


if __name__ == "__main__":
    main()

