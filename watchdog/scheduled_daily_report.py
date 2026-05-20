"""Scheduled daily learning report via read-only Codex analysis."""

from __future__ import annotations

import json
import sqlite3
import urllib.error
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from apscheduler.schedulers.background import BackgroundScheduler

from .codex_adhoc import (
    CodexAdhocConfig,
    _get_json,
    _matching_lines,
    _recent_log_files,
    _redact_text,
    _runtime_summary,
    _safe_json,
    _truncate,
    cap_reply,
    run_codex_adhoc,
)
from .config import WatchdogConfig
from .telegram_bot import _dotnet_ticks, load_sqlite_daily_activity, nt_sqlite_path
from .telegram_notifier import TelegramNotifier


CodexReportRunner = Callable[[CodexAdhocConfig, str], str]


def parse_report_time(value: str) -> Tuple[int, int]:
    parts = str(value or "").strip().split(":")
    if len(parts) != 2:
        return 19, 0
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return 19, 0
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return 19, 0
    return hour, minute


def is_market_session_day(
    local_date: date,
    calendar_name: str = "XNYS",
    *,
    require_calendar: bool = False,
) -> Tuple[bool, str]:
    if local_date.weekday() >= 5:
        return False, "weekend"
    try:
        import pandas_market_calendars as mcal  # type: ignore

        cal = mcal.get_calendar(calendar_name or "XNYS")
        schedule = cal.schedule(start_date=local_date.isoformat(), end_date=local_date.isoformat())
        if schedule.empty:
            return False, f"market_closed:{calendar_name}"
        return True, f"market_open:{calendar_name}"
    except Exception as exc:
        if require_calendar:
            return False, f"calendar_unavailable:{type(exc).__name__}"
        return True, f"weekday_calendar_fallback:{type(exc).__name__}"


def build_daily_report_prompt(window_label: str = "today") -> str:
    return "\n".join(
        [
            f"Daily report: analyze {window_label}'s NinjaTrader trading day.",
            "",
            "Produce a concise Telegram-ready report with these sections:",
            "1. Transaction learnings: summarize what today's fills/closed trades suggest.",
            "2. Strategy improvement ideas: name concrete strategy or signal improvements suggested by transaction behavior, but label hypotheses clearly.",
            "3. NT/watchdog issues: summarize confirmed NinjaTrader, HealthBridge, account/order, connection, and watchdog problems from logs and health context.",
            "4. Action items: give practical next steps, separating verified issues from ideas needing backtest/runtime validation.",
            "",
            "Rules:",
            "- Base claims on the prefetched transaction/log/health context first.",
            "- Include account, instrument, strategy/order names, timestamps, and PnL/trade counts when present.",
            "- If context is thin or Codex cannot infer a strategy cause, say that instead of guessing.",
            "- Keep the answer concise enough for Telegram.",
        ]
    )


def load_daily_transactions(
    db_path: Optional[Path] = None,
    now: Optional[datetime] = None,
    *,
    max_rows: int = 200,
) -> Dict[str, Any]:
    path = db_path or nt_sqlite_path()
    local_now = now.astimezone() if now is not None else datetime.now().astimezone()
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    summary: Dict[str, Any] = {
        "db_path": str(path),
        "window_start_local": day_start.isoformat(timespec="seconds"),
        "window_end_local": day_end.isoformat(timespec="seconds"),
        "executions": [],
        "by_account": {},
        "by_instrument": {},
        "count": 0,
    }
    if not path.exists():
        summary["error"] = "sqlite_missing"
        return summary

    try:
        con = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = _query_execution_rows(con, _dotnet_ticks(day_start), _dotnet_ticks(day_end), max_rows)
        finally:
            con.close()
    except sqlite3.Error as exc:
        summary["error"] = f"sqlite_error:{exc}"
        return summary

    by_account: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"executions": 0, "quantity": 0})
    by_instrument: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"executions": 0, "quantity": 0})
    executions: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        time_ticks = item.get("time_ticks")
        item["time_local"] = _ticks_to_local_iso(time_ticks)
        item.pop("time_ticks", None)
        account = str(item.get("account") or "?")
        instrument = str(item.get("instrument") or item.get("master_instrument") or "?")
        quantity = _safe_int(item.get("quantity"))
        by_account[account]["executions"] += 1
        by_account[account]["quantity"] += quantity
        by_instrument[instrument]["executions"] += 1
        by_instrument[instrument]["quantity"] += quantity
        executions.append(item)

    summary["executions"] = executions
    summary["count"] = len(executions)
    summary["by_account"] = dict(sorted(by_account.items()))
    summary["by_instrument"] = dict(sorted(by_instrument.items()))
    return summary


def build_daily_report_context(
    config: WatchdogConfig,
    now: Optional[datetime] = None,
    *,
    max_chars: int = 18000,
) -> str:
    local_now = now.astimezone() if now is not None else datetime.now().astimezone()
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    workdir = Path(config.telegram_adhoc_codex_workdir or ".")
    sections: List[str] = [
        f"- report_generated_local={local_now.isoformat(timespec='seconds')}",
        f"- report_window_start_local={day_start.isoformat(timespec='seconds')}",
        f"- report_window_end_local={local_now.isoformat(timespec='seconds')}",
        f"- market_calendar={config.daily_report_market_calendar}",
    ]

    try:
        health_payload = _get_json(config.bridge_url.rstrip("/") + config.health_endpoint, timeout_sec=4)
        sections.append(f"- live_healthz={_safe_json(health_payload)}")
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as exc:
        sections.append(f"- live_healthz_error={type(exc).__name__}: {_redact_text(str(exc))}")

    try:
        runtime_payload = _get_json(config.bridge_url.rstrip("/") + config.runtime_snapshot_endpoint, timeout_sec=6)
        sections.append(f"- live_runtime_snapshot_summary={_safe_json(_runtime_summary(runtime_payload), 4000)}")
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as exc:
        sections.append(f"- live_runtime_snapshot_error={type(exc).__name__}: {_redact_text(str(exc))}")

    sqlite_activity = load_sqlite_daily_activity(now=local_now)
    sections.append(f"- sqlite_daily_activity={_safe_json(sqlite_activity, 4000)}")
    sections.append(f"- sqlite_daily_transactions={_safe_json(load_daily_transactions(now=local_now), 8000)}")

    events_path = Path(config.events_log_path)
    if not events_path.is_absolute():
        events_path = workdir / events_path
    event_lines = _tail_jsonl_since(events_path, day_start, max_lines=80)
    if event_lines:
        sections.append("- watchdog_events_today:")
        sections.extend(f"  {line}" for line in event_lines)
    else:
        sections.append("- watchdog_events_today: none found")

    candidate_logs: List[Path] = []
    watchdog_logs = workdir / "watchdog" / "logs"
    if watchdog_logs.exists():
        candidate_logs.extend(_recent_log_files(watchdog_logs.glob("*.log"), day_start))
    nt_logs = Path.home() / "Documents" / "NinjaTrader 8" / "log"
    if nt_logs.exists():
        candidate_logs.extend(_recent_log_files(nt_logs.glob("log.*.txt"), day_start)[:8])
    matches = _matching_lines(candidate_logs, max_lines=100)
    if matches:
        sections.append("- today_error_like_log_lines:")
        sections.extend(f"  {line}" for line in matches)
    else:
        sections.append("- today_error_like_log_lines: none found in today's watchdog/NT logs scanned")

    return _truncate("\n".join(sections), max_chars)


def build_daily_report_question(
    config: WatchdogConfig,
    now: Optional[datetime] = None,
    *,
    market_reason: str = "manual",
) -> str:
    local_now = now.astimezone() if now is not None else datetime.now().astimezone()
    return "\n".join(
        [
            build_daily_report_prompt(local_now.strftime("%Y-%m-%d")),
            "",
            "Prefetched daily context:",
            build_daily_report_context(config, local_now),
            "",
            f"Market-day gate: {market_reason}",
        ]
    )


def build_daily_report_codex_config(config: WatchdogConfig) -> CodexAdhocConfig:
    return CodexAdhocConfig(
        command=config.telegram_adhoc_codex_command,
        workdir=config.telegram_adhoc_codex_workdir,
        data_dir=config.telegram_adhoc_codex_data_dir,
        timeout_sec=config.telegram_adhoc_codex_timeout_sec,
        queue_max=config.telegram_adhoc_codex_queue_max,
        max_reply_chars=config.daily_report_max_reply_chars,
        bridge_url=config.bridge_url,
        health_endpoint=config.health_endpoint,
        runtime_snapshot_endpoint=config.runtime_snapshot_endpoint,
        events_log_path=config.events_log_path,
    )


class ScheduledDailyReportSender:
    def __init__(
        self,
        config: WatchdogConfig,
        notifier: TelegramNotifier,
        *,
        codex_runner: CodexReportRunner = run_codex_adhoc,
        now_provider: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self.config = config
        self.notifier = notifier
        self.codex_runner = codex_runner
        self.now_provider = now_provider
        self.scheduler = BackgroundScheduler()

    def start(self) -> None:
        if not self.config.daily_report_enabled:
            return
        if not self.config.telegram_enabled:
            return
        if not self.config.telegram_bot_token or not self.config.telegram_chat_id:
            return
        hour, minute = parse_report_time(self.config.daily_report_time)
        self.scheduler.add_job(
            self._send_report,
            "cron",
            day_of_week="mon-fri",
            hour=hour,
            minute=minute,
        )
        self.scheduler.start()

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def _send_report(self) -> None:
        now = self.now_provider()
        should_run, reason = is_market_session_day(
            now.date(),
            self.config.daily_report_market_calendar,
            require_calendar=self.config.daily_report_require_market_calendar,
        )
        if not should_run:
            return

        cfg = build_daily_report_codex_config(self.config)
        question = build_daily_report_question(self.config, now, market_reason=reason)
        answer = self.codex_runner(cfg, question)
        message = "Daily learning report\n\n" + cap_reply(answer, self.config.daily_report_max_reply_chars)
        self.notifier.send_message(message)


def _query_execution_rows(
    con: sqlite3.Connection,
    start_ticks: int,
    end_ticks: int,
    max_rows: int,
) -> List[sqlite3.Row]:
    e_cols = _columns(con, "Executions")
    o_cols = _columns(con, "Orders")
    i_cols = _columns(con, "Instruments")
    mi_cols = _columns(con, "MasterInstruments")
    join_master = "MasterInstrument" in i_cols and bool(mi_cols)
    order_join = _order_join_clause(e_cols, o_cols)
    join_orders = bool(order_join)

    select_parts = [
        _select_existing(e_cols, "e", "Id", "execution_id"),
        _select_existing(e_cols, "e", "Instrument", "instrument_id"),
        _select_existing(e_cols, "e", "Time", "time_ticks"),
        _select_existing(e_cols, "e", "MarketPosition", "market_position"),
        _select_existing(e_cols, "e", "Price", "price"),
        _select_existing(e_cols, "e", "Quantity", "quantity"),
        _select_existing(e_cols, "e", "Commission", "commission"),
        _select_existing(e_cols, "e", "Fee", "fee"),
        "a.Name as account",
    ]
    if "FullName" in i_cols:
        select_parts.append("i.FullName as instrument")
    elif "Name" in i_cols:
        select_parts.append("i.Name as instrument")
    elif join_master and "Name" in mi_cols:
        select_parts.append("mi.Name as instrument")
    else:
        select_parts.append("e.Instrument as instrument")
    if join_master and "Name" in mi_cols:
        select_parts.append("mi.Name as master_instrument")
    if join_orders and "Name" in o_cols:
        select_parts.append("o.Name as order_name")
    if join_orders and "OrderId" in o_cols:
        select_parts.append("o.OrderId as order_id")
    if join_orders and "OrderAction" in o_cols:
        select_parts.append("o.OrderAction as order_action")
    if join_orders and "OrderState" in o_cols:
        select_parts.append("o.OrderState as order_state")

    joins = [
        "left join Accounts a on a.Id = e.Account",
        "left join Instruments i on i.Id = e.Instrument",
    ]
    if join_master:
        joins.append("left join MasterInstruments mi on mi.Id = i.MasterInstrument")
    if join_orders:
        joins.append(order_join)

    sql = (
        "select "
        + ", ".join(part for part in select_parts if part)
        + " from Executions e "
        + " ".join(joins)
        + " where e.Time >= ? and e.Time < ? order by e.Time, e.Id limit ?"
    )
    return con.execute(sql, (start_ticks, end_ticks, max(1, int(max_rows)))).fetchall()


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in con.execute(f"pragma table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _select_existing(columns: set[str], alias: str, column: str, out_name: str) -> str:
    if column not in columns:
        return f"null as {out_name}"
    return f"{alias}.[{column}] as {out_name}"


def _order_join_clause(execution_columns: set[str], order_columns: set[str]) -> str:
    if not order_columns:
        return ""
    if "Order" in execution_columns and "Id" in order_columns:
        return "left join Orders o on o.Id = e.[Order]"
    if "OrderId" in execution_columns and "OrderId" in order_columns:
        return "left join Orders o on o.OrderId = e.OrderId"
    if "OrderId" in execution_columns and "Id" in order_columns:
        return "left join Orders o on o.Id = e.OrderId"
    return ""


def _ticks_to_local_iso(value: Any) -> str:
    try:
        ticks = int(value or 0)
    except (TypeError, ValueError):
        return ""
    seconds = ticks / 10000000
    dt = datetime(1, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return dt.astimezone().isoformat(timespec="seconds")


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _tail_jsonl_since(path: Path, since: datetime, max_lines: int) -> List[str]:
    if not path.exists():
        return []
    out: List[str] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines[-1000:]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        timestamp = str(event.get("time_utc") or event.get("timestamp") or "")
        if timestamp:
            try:
                event_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone()
                if event_time < since:
                    continue
            except ValueError:
                pass
        out.append(_redact_text(line))
    return out[-max_lines:]
