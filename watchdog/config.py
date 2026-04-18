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
    poll_interval_sec: int = 60
    startup_grace_sec: int = 90
    mainthread_stuck_sec: int = 12
    unstable_cycles_before_recovery: int = 3
    reconnect_attempt_limit: int = 3
    notification_cooldown_sec: int = 900
    reconnect_cooldown_sec: int = 20
    restart_cooldown_sec: int = 120
    max_restarts_per_hour: int = 2
    recovery_only_when_flat: bool = False
    nt_executable_path: str = r"C:\Program Files\NinjaTrader 8\bin64\NinjaTrader.exe"
    nt_process_name: str = "NinjaTrader"
    nt_start_args: List[str] = field(default_factory=list)
    process_detect_fallback: bool = True
    snapshot_path: str = "watchdog/state/last_good_snapshot.json"
    events_log_path: str = "watchdog/logs/health_events.jsonl"
    telegram_enabled: bool = True
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_parse_mode: str = ""


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


def load_config(path: str) -> WatchdogConfig:
    cfg = WatchdogConfig()
    base_dir = Path.cwd()

    raw = {}
    cfg_path = Path(path)
    if cfg_path.exists():
        cfg_path = cfg_path.resolve()
        if cfg_path.parent.name.lower() == "watchdog":
            base_dir = cfg_path.parent.parent
        else:
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
        "unstable_cycles_before_recovery",
        _to_int(os.getenv("WATCHDOG_UNSTABLE_CYCLES"), cfg.unstable_cycles_before_recovery),
    )

    cfg.bridge_url = cfg.bridge_url.rstrip("/")
    snapshot_path = Path(cfg.snapshot_path)
    events_log_path = Path(cfg.events_log_path)
    if not snapshot_path.is_absolute():
        snapshot_path = (base_dir / snapshot_path).resolve()
    if not events_log_path.is_absolute():
        events_log_path = (base_dir / events_log_path).resolve()
    cfg.snapshot_path = str(snapshot_path)
    cfg.events_log_path = str(events_log_path)
    return cfg

