from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
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
            "date_label": local_now.date().isoformat(),
            "time_label": local_now.strftime("%H:%M"),
            "user_id": int(user_id or 0),
            "source": source,
            "text": clean,
            "path": str(self.notes_markdown_path),
        }
        self._append_markdown_items([clean], local_now=local_now, source_label="note")
        return item

    def read_notes(self, day: Optional[date] = None, *, max_items: int = 50) -> List[Dict[str, Any]]:
        target = day or self.now_provider().astimezone().date()
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

    def read_recent_notes(self, *, days: int = 7, max_items: int = 100) -> List[Dict[str, Any]]:
        local_today = self.now_provider().astimezone().date()
        window_days = max(1, int(days or 1))
        out: List[Dict[str, Any]] = []
        for offset in range(window_days - 1, -1, -1):
            target = local_today - timedelta(days=offset)
            out.extend(self.read_notes(target, max_items=max_items))
            out.extend(self.read_markdown_notes(target, max_items=max_items))
        return out[-max(1, int(max_items)) :]

    def read_markdown_notes(
        self,
        day: Optional[date] = None,
        *,
        max_items: int = 50,
    ) -> List[Dict[str, Any]]:
        target = day or self.now_provider().astimezone().date()
        path = self.notes_markdown_path
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []

        out: List[Dict[str, Any]] = []
        current_day: Optional[date] = None
        legacy_time = ""
        for raw in lines:
            line = raw.strip()
            header = re.match(r"^##\s+(\d{4}-\d{2}-\d{2})(?:\s+(\d{2}:\d{2}))?", line)
            if header:
                try:
                    current_day = date.fromisoformat(header.group(1))
                except ValueError:
                    current_day = None
                legacy_time = header.group(2) or ""
                continue
            if current_day != target or not line.startswith("- "):
                continue

            text = line[2:].strip()
            time_label = legacy_time or "time?"
            source = "telegram_review" if legacy_time else "telegram"
            modern = re.match(r"^(\d{2}:\d{2})\s+\[([^\]]+)\]\s+(.+)", text)
            if modern:
                time_label = modern.group(1)
                source = modern.group(2)
                text = modern.group(3).strip()
            if text:
                out.append(
                    {
                        "date_label": current_day.isoformat(),
                        "time_label": time_label,
                        "source": source,
                        "text": text,
                    }
                )
        return out[-max(1, int(max_items)) :]

    def read_review_action_items(
        self,
        day: Optional[date] = None,
        *,
        max_items: int = 50,
    ) -> List[Dict[str, Any]]:
        return [
            item
            for item in self.read_markdown_notes(day, max_items=max_items)
            if str(item.get("source") or "").lower() in {"review", "telegram_review"}
        ]

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
        existing_review_items = {
            _normalize_note_text(str(item.get("text") or ""))
            for item in self.read_recent_notes(days=7, max_items=500)
            if str(item.get("source") or "").lower() in {"review", "telegram_review"}
        }
        deduped_items: List[str] = []
        skipped = 0
        for item in clean_items:
            key = _normalize_note_text(item)
            if key in existing_review_items:
                skipped += 1
                continue
            existing_review_items.add(key)
            deduped_items.append(item)
        if not deduped_items:
            return {"path": str(self.notes_markdown_path), "items": 0, "skipped": skipped}

        self._append_markdown_items(deduped_items, local_now=local_now, source_label="review")
        return {"path": str(self.notes_markdown_path), "items": len(deduped_items), "skipped": skipped}

    def _append_markdown_items(
        self,
        items: List[str],
        *,
        local_now: datetime,
        source_label: str,
    ) -> None:
        clean_items = [str(item or "").strip() for item in items if str(item or "").strip()]
        if not clean_items:
            return
        day_label = local_now.date().isoformat()
        time_label = local_now.strftime("%H:%M")
        self.notes_markdown_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = self.notes_markdown_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            existing = ""

        lines: List[str] = []
        if existing and not existing.endswith("\n"):
            lines.append("")
        if f"## {day_label}" not in existing:
            if existing:
                lines.append("")
            lines.append(f"## {day_label}")
            lines.append("")
        lines.extend(f"- {time_label} [{source_label}] {item}" for item in clean_items)
        lines.append("")

        with self.notes_markdown_path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines))


def format_notes(notes: List[Dict[str, Any]], *, label: str = "today") -> str:
    if not notes:
        return f"No notes for {label}."
    lines = [f"Notes for {label}:"]
    current_date = ""
    for item in notes:
        stamp = str(item.get("time_utc") or "")
        time_label = str(item.get("time_label") or "")
        date_label = str(item.get("date_label") or "")
        text = str(item.get("text") or "")
        note_date = _format_note_date(stamp, date_label)
        if note_date != current_date:
            lines.append("")
            lines.append(note_date)
            current_date = note_date
        time_part = _format_note_time(stamp, time_label)
        lines.append(f"- {time_part} {text}")
    return "\n".join(lines)


def _format_note_date(stamp: str, date_label: str) -> str:
    if date_label:
        return date_label
    if len(stamp) >= 10:
        return stamp[:10]
    return "Unknown date"


def _format_note_time(stamp: str, time_label: str) -> str:
    if len(stamp) >= 16:
        return f"{stamp[11:16]}Z"
    if time_label:
        return time_label
    return "time?"


def _normalize_note_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().casefold())


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
