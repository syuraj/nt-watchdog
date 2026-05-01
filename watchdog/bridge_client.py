from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .config import WatchdogConfig


class BridgeClient:
    def __init__(self, config: WatchdogConfig) -> None:
        self.config = config

    def get_json(self, endpoint: str, timeout_sec: int = 5) -> Dict[str, Any]:
        req = urllib.request.Request(
            self.config.bridge_url + endpoint,
            method="GET",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            payload = resp.read().decode("utf-8")
        return json.loads(payload)

    def post_json(self, endpoint: str, body: Optional[Dict[str, Any]] = None, timeout_sec: int = 10) -> Dict[str, Any]:
        data = json.dumps(body or {}).encode("utf-8")
        req = urllib.request.Request(
            self.config.bridge_url + endpoint,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            payload = resp.read().decode("utf-8")
        return json.loads(payload)

    def safe_health(self) -> Dict[str, Any]:
        try:
            return self.get_json(self.config.health_endpoint, timeout_sec=4)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, socket.timeout, json.JSONDecodeError) as exc:
            return {"status": "down", "error": str(exc)}

    def safe_runtime_snapshot(self) -> Dict[str, Any]:
        try:
            return self.get_json(self.config.runtime_snapshot_endpoint, timeout_sec=6)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, socket.timeout, json.JSONDecodeError) as exc:
            return {"error": str(exc)}

    def safe_daily_pnl(self, timeout_sec: int = 6) -> List[Dict[str, Any]]:
        try:
            resp = self.get_json("/daily_pnl", timeout_sec=timeout_sec)
            return resp if isinstance(resp, list) else []
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, socket.timeout, json.JSONDecodeError):
            return []

    def enable_all_strategies(self, timeout_sec: int = 15) -> Dict[str, Any]:
        try:
            return self.post_json("/strategies/enable_all", body={}, timeout_sec=timeout_sec)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, socket.timeout, json.JSONDecodeError) as exc:
            return {"method": "uia_keypress", "toggled": 0, "error": str(exc)}

    def disable_all_strategies(self, timeout_sec: int = 15) -> Dict[str, Any]:
        try:
            return self.post_json("/strategies/disable_all", body={}, timeout_sec=timeout_sec)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, socket.timeout, json.JSONDecodeError) as exc:
            return {"method": "reflection_setstate", "toggled": 0, "count": 0, "error": str(exc)}

    def dismiss_blocking_dialogs(self, timeout_sec: int = 10) -> Dict[str, Any]:
        try:
            return self.post_json("/dialogs/dismiss", body={}, timeout_sec=timeout_sec)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, socket.timeout, json.JSONDecodeError) as exc:
            return {"dismissed": 0, "clicked": [], "error": str(exc)}


    def recover_reconnect(self, flatten_first: bool, connection_names: Optional[List[str]] = None) -> Dict[str, Any]:
        endpoint = (
            self.config.flatten_then_reconnect_endpoint
            if flatten_first
            else self.config.reconnect_endpoint
        )
        body: Dict[str, Any] = {}
        names = connection_names if connection_names is not None else self.config.connection_names
        if names:
            body["connection_names"] = names
        try:
            return self.post_json(endpoint, body=body, timeout_sec=12)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, socket.timeout, json.JSONDecodeError) as exc:
            return {"success": False, "error": str(exc), "action": endpoint}

