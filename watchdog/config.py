from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


@dataclass
class WatchdogConfig:
    bridge_url: str = "http://localhost:8899"
    health_endpoint: str = "/healthz"
    runtime_snapshot_endpoint: str = "/runtime_snapshot"
    reconnect_endpoint: str = "/recover/reconnect"
    flatten_then_reconnect_endpoint: str = "/recover/flatten_then_reconnect"
    connection_names: List[str] = field(default_factory=list)
    poll_interval_sec: int = 60
    startup_grace_sec: int = 90
    mainthread_stuck_sec: int = 12
    unstable_cycles_before_recovery: int = 3
    reconnect_attempt_limit: int = 10
    notification_cooldown_sec: int = 900
    no_connections_recovery_cooldown_sec: int = 300
    restart_cooldown_sec: int = 120
    max_restarts_per_hour: int = 2
    recovery_only_when_flat: bool = False
    nt_executable_path: str = r"C:\Program Files\NinjaTrader 8\bin\NinjaTrader.exe"
    nt_process_name: str = "NinjaTrader"
    nt_username: str = ""
    nt_password: str = ""
    process_detect_fallback: bool = True
    snapshot_path: str = "watchdog/state/last_good_snapshot.json"
    events_log_path: str = "watchdog/logs/health_events.jsonl"
    telegram_enabled: bool = True
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_parse_mode: str = ""
    # Telegram user IDs allowed to send commands to the bot. Leave empty to
    # disable the command bot even if notifier is enabled. Use user IDs (not
    # chat IDs) so group-chat members don't inherit access.
    telegram_allowed_user_ids: List[int] = field(default_factory=list)
    telegram_adhoc_codex_enabled: bool = True
    telegram_adhoc_codex_command: str = "codex"
    telegram_adhoc_codex_workdir: str = "."
    telegram_adhoc_codex_data_dir: str = "watchdog/state/codex_adhoc"
    telegram_adhoc_codex_timeout_sec: int = 120
    telegram_adhoc_codex_queue_max: int = 2
    telegram_adhoc_codex_max_reply_chars: int = 3500
    daily_report_enabled: bool = True
    daily_report_time: str = "19:00"
    daily_report_market_calendar: str = "XNYS"
    daily_report_require_market_calendar: bool = False
    daily_report_max_reply_chars: int = 3500
    notes_enabled: bool = True
    notes_dir: str = "watchdog/state/notes"
    notes_max_chars: int = 2000


def _to_bool(value: str, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _to_int(value: str, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _set_if_present(obj: WatchdogConfig, key: str, value) -> None:
    if value is None:
        return
    if not hasattr(obj, key):
        return
    setattr(obj, key, value)


def _to_list(value, default: List[str]) -> List[str]:
    if value is None:
        return list(default)
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return list(default)


def _to_int_list(value, default: List[int]) -> List[int]:
    if value is None:
        return list(default)
    raw_items: List[str]
    if isinstance(value, list):
        raw_items = [str(v).strip() for v in value]
    elif isinstance(value, str):
        raw_items = [part.strip() for part in value.split(",")]
    else:
        return list(default)
    out: List[int] = []
    for item in raw_items:
        if not item:
            continue
        try:
            out.append(int(item))
        except ValueError:
            continue
    return out


def load_config(path: str) -> WatchdogConfig:
    cfg = WatchdogConfig()
    base_dir = Path.cwd()

    raw = {}
    cfg_path = Path(path)
    if cfg_path.exists():
        cfg_path = cfg_path.resolve()
        base_dir = cfg_path.parent
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("PyYAML is required to read config.yaml. Install with: pip install pyyaml") from exc
        loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError("Configuration root must be a mapping/object.")
        raw = loaded

    for key, value in raw.items():
        _set_if_present(cfg, key, value)

    _set_if_present(cfg, "bridge_url", os.getenv("NT8_BRIDGE_URL"))
    _set_if_present(cfg, "nt_executable_path", os.getenv("NT8_EXECUTABLE_PATH"))
    _set_if_present(cfg, "nt_username", os.getenv("WATCHDOG_NT_USERNAME"))
    _set_if_present(cfg, "nt_password", os.getenv("WATCHDOG_NT_PASSWORD"))
    _set_if_present(cfg, "telegram_bot_token", os.getenv("TELEGRAM_BOT_TOKEN"))
    _set_if_present(cfg, "telegram_chat_id", os.getenv("TELEGRAM_CHAT_ID"))
    _set_if_present(cfg, "telegram_enabled", _to_bool(os.getenv("TELEGRAM_ENABLED"), cfg.telegram_enabled))
    _set_if_present(cfg, "poll_interval_sec", _to_int(os.getenv("WATCHDOG_POLL_INTERVAL_SEC"), cfg.poll_interval_sec))
    _set_if_present(
        cfg,
        "notification_cooldown_sec",
        _to_int(os.getenv("WATCHDOG_NOTIFICATION_COOLDOWN_SEC"), cfg.notification_cooldown_sec),
    )
    _set_if_present(
        cfg,
        "no_connections_recovery_cooldown_sec",
        _to_int(
            os.getenv("WATCHDOG_NO_CONNECTIONS_RECOVERY_COOLDOWN_SEC"),
            cfg.no_connections_recovery_cooldown_sec,
        ),
    )
    _set_if_present(
        cfg,
        "unstable_cycles_before_recovery",
        _to_int(os.getenv("WATCHDOG_UNSTABLE_CYCLES"), cfg.unstable_cycles_before_recovery),
    )
    _set_if_present(
        cfg,
        "connection_names",
        _to_list(os.getenv("WATCHDOG_CONNECTION_NAMES"), cfg.connection_names),
    )
    _set_if_present(
        cfg,
        "telegram_allowed_user_ids",
        _to_int_list(os.getenv("TELEGRAM_ALLOWED_USER_IDS"), cfg.telegram_allowed_user_ids),
    )
    _set_if_present(
        cfg,
        "telegram_adhoc_codex_enabled",
        _to_bool(os.getenv("TELEGRAM_ADHOC_CODEX_ENABLED"), cfg.telegram_adhoc_codex_enabled),
    )
    _set_if_present(cfg, "telegram_adhoc_codex_command", os.getenv("TELEGRAM_ADHOC_CODEX_COMMAND"))
    _set_if_present(cfg, "telegram_adhoc_codex_workdir", os.getenv("TELEGRAM_ADHOC_CODEX_WORKDIR"))
    _set_if_present(cfg, "telegram_adhoc_codex_data_dir", os.getenv("TELEGRAM_ADHOC_CODEX_DATA_DIR"))
    _set_if_present(
        cfg,
        "telegram_adhoc_codex_timeout_sec",
        _to_int(os.getenv("TELEGRAM_ADHOC_CODEX_TIMEOUT_SEC"), cfg.telegram_adhoc_codex_timeout_sec),
    )
    _set_if_present(
        cfg,
        "telegram_adhoc_codex_queue_max",
        _to_int(os.getenv("TELEGRAM_ADHOC_CODEX_QUEUE_MAX"), cfg.telegram_adhoc_codex_queue_max),
    )
    _set_if_present(
        cfg,
        "telegram_adhoc_codex_max_reply_chars",
        _to_int(os.getenv("TELEGRAM_ADHOC_CODEX_MAX_REPLY_CHARS"), cfg.telegram_adhoc_codex_max_reply_chars),
    )
    _set_if_present(
        cfg,
        "daily_report_enabled",
        _to_bool(os.getenv("DAILY_REPORT_ENABLED"), cfg.daily_report_enabled),
    )
    _set_if_present(cfg, "daily_report_time", os.getenv("DAILY_REPORT_TIME"))
    _set_if_present(cfg, "daily_report_market_calendar", os.getenv("DAILY_REPORT_MARKET_CALENDAR"))
    _set_if_present(
        cfg,
        "daily_report_require_market_calendar",
        _to_bool(os.getenv("DAILY_REPORT_REQUIRE_MARKET_CALENDAR"), cfg.daily_report_require_market_calendar),
    )
    _set_if_present(
        cfg,
        "daily_report_max_reply_chars",
        _to_int(os.getenv("DAILY_REPORT_MAX_REPLY_CHARS"), cfg.daily_report_max_reply_chars),
    )
    _set_if_present(
        cfg,
        "notes_enabled",
        _to_bool(os.getenv("NOTES_ENABLED"), cfg.notes_enabled),
    )
    _set_if_present(cfg, "notes_dir", os.getenv("NOTES_DIR"))
    _set_if_present(
        cfg,
        "notes_max_chars",
        _to_int(os.getenv("NOTES_MAX_CHARS"), cfg.notes_max_chars),
    )

    cfg.bridge_url = cfg.bridge_url.rstrip("/")
    cfg.connection_names = _to_list(cfg.connection_names, [])
    cfg.telegram_allowed_user_ids = _to_int_list(cfg.telegram_allowed_user_ids, [])
    snapshot_path = Path(cfg.snapshot_path)
    events_log_path = Path(cfg.events_log_path)
    if not snapshot_path.is_absolute():
        snapshot_path = (base_dir / snapshot_path).resolve()
    if not events_log_path.is_absolute():
        events_log_path = (base_dir / events_log_path).resolve()
    cfg.snapshot_path = str(snapshot_path)
    cfg.events_log_path = str(events_log_path)
    workdir_path = Path(cfg.telegram_adhoc_codex_workdir or str(base_dir))
    data_dir_path = Path(cfg.telegram_adhoc_codex_data_dir)
    notes_dir_path = Path(cfg.notes_dir)
    if not workdir_path.is_absolute():
        workdir_path = (base_dir / workdir_path).resolve()
    if not data_dir_path.is_absolute():
        data_dir_path = (base_dir / data_dir_path).resolve()
    if not notes_dir_path.is_absolute():
        notes_dir_path = (base_dir / notes_dir_path).resolve()
    cfg.telegram_adhoc_codex_workdir = str(workdir_path)
    cfg.telegram_adhoc_codex_data_dir = str(data_dir_path)
    cfg.notes_dir = str(notes_dir_path)
    return cfg

