# AGENTS.md

Guidance for coding agents (Claude Code, Cursor, Codex, Aider, etc.) working in this repository.

## Project Purpose

Self-healing health monitor for NinjaTrader 8 on a Windows VPS. Two components:

- **`HealthBridge.cs`** — NT8 AddOn (C#, compiled inside NinjaTrader). In-process `HttpListener` on `localhost:8899` exposing health/recovery endpoints. Runtime messages go to NT Output window (`[HealthBridge] ...`).
- **`watchdog/`** — external Python monitor. Polls bridge, detects incidents, runs staged recovery, sends Telegram alerts.

Watchdog decisions use **NT/HealthBridge connection state only** — no external internet probing.

## Bridge Endpoints (port 8899)

- `GET /health` — liveness
- `GET /healthz` — detailed (stale UI, connection stability, blocking windows)
- `GET /runtime_snapshot` — accounts, positions, strategies
- `POST /recover/reconnect`
- `POST /recover/flatten_then_reconnect`

Legacy bridge on `8888` (if still loaded) exposes `POST /compile` for recompile checks.

## Common Commands

From repo root:

```bash
python scripts/manage_watchdog.py setup --bridge-url http://localhost:8899 --nt-executable-path "C:\Path\To\NinjaTrader.exe"
python scripts/manage_watchdog.py run
python scripts/manage_watchdog.py status            # concise
python scripts/manage_watchdog.py status --verbose
python scripts/manage_watchdog.py status --json
python scripts/manage_watchdog.py smoke-test --cleanup
```

Direct module run:

```bash
python -m watchdog.monitor --config watchdog/config.yaml
python -m watchdog.monitor --config watchdog/config.yaml --max-cycles 3   # finite for smoke
```

Startup on login (no admin):

```bash
python scripts/manage_watchdog.py install-startup
python scripts/manage_watchdog.py trigger-startup
python scripts/manage_watchdog.py remove-startup
```

Telegram secrets via env (preferred over config.yaml):

```bash
setx TELEGRAM_BOT_TOKEN "<token>"
setx TELEGRAM_CHAT_ID "<chat_id>"
```

Optional env overrides:

```bash
setx WATCHDOG_NO_CONNECTIONS_RECOVERY_COOLDOWN_SEC "300"
setx WATCHDOG_NOTIFICATION_COOLDOWN_SEC "900"
```

## Verification Checklist

Run before declaring work done:

```bash
python -m compileall scripts watchdog
python -m unittest watchdog.tests.test_recovery watchdog.tests.test_telegram_notifier -v
```

Single test method:

```bash
python -m unittest watchdog.tests.test_recovery.TestRecoveryManager.<method_name> -v
```

Live bridge (when NT running):

- `GET http://localhost:8899/health`
- `GET http://localhost:8899/healthz`
- `POST http://localhost:8888/compile` (legacy bridge, if still loaded)

## Recovery Policy

Staged with circuit breaker:

1. **Reconnect first** — `/recover/reconnect`, honors `reconnect_cooldown_sec`, `reconnect_attempt_limit`.
2. **Restart fallback** — only after repeated reconnect failure. Gated by `max_restarts_per_hour`, `restart_cooldown_sec`.
3. **Flatten-then-reconnect** — when open positions detected.
4. **`no_connections_detected` special-case** — reconnect-only with `no_connections_recovery_cooldown_sec`. **Never** escalates to NT restart from this reason alone.

Alert dedupe: repeats within `notification_cooldown_sec` logged as `alert_skipped=True`.

Default bridge port is `8899` (intentionally different from legacy `8888`).

## Architecture Notes

- `watchdog/monitor.py` — main loop. Single-instance lock via `msvcrt` (`watchdog.lock`). Cycles at `poll_interval_sec` (default 60s). Bootstraps NT process if down after `startup_grace_sec`.
- `watchdog/recovery.py` — `RecoveryManager.handle_cycle()` is the decision brain. Returns `{action, reason, alert_*}`.
- `watchdog/bridge_client.py` — `safe_*` wrappers never raise.
- `watchdog/nt_process.py` — start/detect NT by name or exe path.
- `watchdog/state_store.py` — appends `logs/health_events.jsonl`, persists `state/last_good_snapshot.json` and `state/runtime_state.json` (restart history, incident id, reconnect-failure count).
- `watchdog/strategy_ui_restore.py` — NT8 strategy re-enable APIs are limited; currently requests manual restore unless UI automation backend is wired in.
- `watchdog/telegram_notifier.py` — exposes `last_error` for failure visibility.

Key paths:

- Event log: `watchdog/logs/health_events.jsonl`
- Snapshot: `watchdog/state/last_good_snapshot.json`
- Runtime counters: `watchdog/state/runtime_state.json`
- NT logs: `C:\Users\sshrestha\Documents\NinjaTrader 8\log\log.*.txt`

## Editing Rules

- Prefer **Python over PowerShell** for new automation. PS scripts in `scripts/` are thin wrappers only.
- Keep `scripts/manage_watchdog.py` as the main operational CLI.
- **Do not reintroduce Task Scheduler logic** unless explicitly requested — startup uses user-login launcher, no admin.
- Keep `status` concise by default; detail behind `--verbose` / `--json`.
- Alert send failures must be visible in both terminal output and event log.

## Installing Bridge Changes

`HealthBridge.cs` edits do not take effect until copied into NT AddOns folder and NinjaScript recompiled:

```
C:\Users\<you>\Documents\NinjaTrader 8\bin\Custom\AddOns\HealthBridge.cs
```

Then: NinjaTrader → NinjaScript → Compile (or `POST /compile` on legacy 8888 bridge if loaded).
