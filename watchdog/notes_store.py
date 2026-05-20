from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .codex_adhoc import _redact_text
from .config import WatchdogConfig


class NotesStore:
    def __init__(
        self,
        config: WatchdogConfig,
        *,
        now_provider=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.config = config
        self.notes_dir = Path(config.notes_dir)
        self.now_provider = now_provider

    def append_note(self, text: str, *, user_id: int = 0, source: str = "telegram") -> Dict[str, Any]:
        clean = _redact_text(str(text or "").strip())
        if not clean:
            raise ValueError("empty_note")
        max_chars = max(1, int(self.config.notes_max_chars or 1))
        if len(clean) > max_chars:
            clean = clean[:max_chars].rstrip()
        observed_now = self.now_provider()
        local_now = observed_now.astimezone()
        now = observed_now.astimezone(timezone.utc)
        item: Dict[str, Any] = {
            "time_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "user_id": int(user_id or 0),
            "source": source,
            "text": clean,
        }
        path = self.path_for_date(local_now.date())
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(item, sort_keys=True, ensure_ascii=False) + "\n")
        return item

    def read_notes(self, day: Optional[date] = None, *, max_items: int = 50) -> List[Dict[str, Any]]:
        target = day or datetime.now().astimezone().date()
        path = self.path_for_date(target)
        if not path.exists():
            return []
        out: List[Dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        for line in lines[-max(1, int(max_items)) :]:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                out.append(item)
        return out

    def path_for_date(self, day: date) -> Path:
        return self.notes_dir / f"{day.isoformat()}.jsonl"


def format_notes(notes: List[Dict[str, Any]], *, label: str = "today") -> str:
    if not notes:
        return f"No notes for {label}."
    lines = [f"Notes for {label}:"]
    for item in notes:
        stamp = str(item.get("time_utc") or "")
        text = str(item.get("text") or "")
        time_part = stamp[11:16] + "Z" if len(stamp) >= 16 else "time?"
        lines.append(f"- {time_part} {text}")
    return "\n".join(lines)
