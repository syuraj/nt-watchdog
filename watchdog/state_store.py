from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from .config import WatchdogConfig


class StateStore:
    def __init__(self, config: WatchdogConfig) -> None:
        self.config = config
        self.snapshot_file = Path(config.snapshot_path)
        self.events_file = Path(config.events_log_path)
        self.runtime_state_file = self.snapshot_file.with_name("runtime_state.json")
        self.snapshot_file.parent.mkdir(parents=True, exist_ok=True)
        self.events_file.parent.mkdir(parents=True, exist_ok=True)

    def _read_json(self, path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def _write_json(self, path: Path, payload: Dict[str, Any]) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temp.replace(path)

    def load_snapshot(self) -> Dict[str, Any]:
        return self._read_json(self.snapshot_file, default={})

    def save_snapshot(self, payload: Dict[str, Any]) -> None:
        self._write_json(self.snapshot_file, payload)

    def load_runtime_state(self) -> Dict[str, Any]:
        return self._read_json(
            self.runtime_state_file,
            default={"restarts": [], "reconnect_failures": 0, "last_incident_id": ""},
        )

    def save_runtime_state(self, payload: Dict[str, Any]) -> None:
        self._write_json(self.runtime_state_file, payload)

    def append_event(self, event: Dict[str, Any]) -> None:
        line = json.dumps(
            {
                "time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                **event,
            },
            sort_keys=True,
        )
        with self.events_file.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    @staticmethod
    def prune_restart_history(restarts: List[str], window_sec: int = 3600) -> List[str]:
        now = datetime.now(timezone.utc)
        kept: List[str] = []
        for stamp in restarts:
            try:
                ts = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except Exception:
                continue
            if (now - ts).total_seconds() <= window_sec:
                kept.append(stamp)
        return kept

