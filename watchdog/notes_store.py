from __future__ import annotations

import json
import re
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
        self.notes_markdown_path = Path(config.notes_markdown_path)
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

    def append_review_action_items(
        self,
        report_text: str,
        *,
        user_id: int = 0,
        source: str = "telegram_review",
    ) -> Dict[str, Any]:
        items = extract_action_items(report_text)
        if not items:
            return {"path": str(self.notes_markdown_path), "items": 0}

        observed_now = self.now_provider()
        local_now = observed_now.astimezone()
        max_chars = max(1, int(self.config.notes_max_chars or 1))
        clean_items = [_redact_text(item).strip()[:max_chars].rstrip() for item in items]
        clean_items = [item for item in clean_items if item]
        if not clean_items:
            return {"path": str(self.notes_markdown_path), "items": 0}

        header = local_now.strftime("## %Y-%m-%d %H:%M %Z")
        detail = f"source={source}"
        if user_id:
            detail += f", user_id={int(user_id)}"
        lines = [header, f"_Review action items ({detail})_", ""]
        lines.extend(f"- {item}" for item in clean_items)
        lines.append("")

        self.notes_markdown_path.parent.mkdir(parents=True, exist_ok=True)
        with self.notes_markdown_path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines))
        return {"path": str(self.notes_markdown_path), "items": len(clean_items)}


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


def extract_action_items(report_text: str) -> List[str]:
    lines = str(report_text or "").splitlines()
    out: List[str] = []
    in_action_section = False

    for raw in lines:
        line = raw.strip()
        normalized = _normalize_heading(line)
        if not in_action_section:
            if "action item" in normalized or normalized in {"next steps", "actions"}:
                in_action_section = True
                remainder = _heading_remainder(line)
                if remainder:
                    out.append(_clean_action_item(remainder))
            continue

        if not line:
            continue
        if _looks_like_section_heading(line):
            break
        cleaned = _clean_action_item(line)
        if cleaned:
            out.append(cleaned)

    return [item for item in out if item]


def _normalize_heading(line: str) -> str:
    text = re.sub(r"^[#>*\-\s\d.()\[\]]+", "", line.strip())
    text = text.strip("*_`:- ").lower()
    text = re.sub(r"[^a-z0-9/& ]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _heading_remainder(line: str) -> str:
    if ":" not in line:
        return ""
    before, after = line.split(":", 1)
    if "action item" not in _normalize_heading(before):
        return ""
    return after.strip()


def _looks_like_section_heading(line: str) -> bool:
    if _is_list_item(line):
        return False
    normalized = _normalize_heading(line)
    if not normalized:
        return False
    known = (
        "summary",
        "transaction learnings",
        "strategy improvement ideas",
        "strategy ideas",
        "nt/watchdog issues",
        "watchdog issues",
        "issues",
        "notes",
        "evidence",
    )
    return any(normalized == value or normalized.startswith(value + " ") for value in known)


def _is_list_item(line: str) -> bool:
    return bool(re.match(r"^([-*+]|\d+[.)]|\[[ xX]\]|-\s+\[[ xX]\])\s+", line.strip()))


def _clean_action_item(line: str) -> str:
    text = re.sub(r"^[-*+]\s+\[[ xX]\]\s+", "", line.strip())
    text = re.sub(r"^\[[ xX]\]\s+", "", text)
    text = re.sub(r"^[-*+]\s+", "", text)
    text = re.sub(r"^\d+[.)]\s+", "", text)
    return text.strip()
