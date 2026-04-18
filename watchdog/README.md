# NT8 Watchdog

External watchdog service for NinjaTrader 8 running on a Windows VPS.

## Features
- Polls `HealthBridge` endpoints (`/healthz`, `/runtime_snapshot`).
- Detects stuck UI, disconnected/unstable connections, and bridge downtime.
- Uses NT/HealthBridge connection state only (no external internet probe checks).
- Checks once per minute by default (`poll_interval_sec: 60`) to reduce log spam.
- Reconnect-first recovery policy.
- Open-position policy: flatten then recover.
- Restart fallback with restart circuit breaker.
- Persists last known good runtime snapshot.
- Sends incident notifications to Telegram.
- Deduplicates repeat incident alerts during cooldown (`notification_cooldown_sec`).

## Setup
Python-first setup (recommended):
- `python scripts/manage_watchdog.py setup --bridge-url http://localhost:8899 --nt-executable-path "C:\Path\To\NinjaTrader.exe"`

Then set Telegram secrets (recommended via env vars):
   - `setx TELEGRAM_BOT_TOKEN "<token>"`
   - `setx TELEGRAM_CHAT_ID "<chat_id>"`

PowerShell helper scripts are still available as thin wrappers around the Python CLI, but Python commands are the preferred path.

## Run
- `python -m watchdog.monitor --config watchdog/config.yaml`
- or `python scripts/manage_watchdog.py run`
- check combined status: `python scripts/manage_watchdog.py status`
- For smoke tests: `python -m watchdog.monitor --config watchdog/config.yaml --max-cycles 3`
- or `python scripts/manage_watchdog.py smoke-test --cleanup`

## Logs and State
- Runtime events: `watchdog/logs/health_events.jsonl`
- Last good snapshot: `watchdog/state/last_good_snapshot.json`
- Runtime counters: `watchdog/state/runtime_state.json`

## Startup on VPS
Start on user login (no admin required):
- Install startup launcher: `python scripts/manage_watchdog.py install-startup`
- Remove startup launcher: `python scripts/manage_watchdog.py remove-startup`
- Trigger startup now (without relogin):
  - `python scripts/manage_watchdog.py trigger-startup`

## Note on Strategy Re-enable
NT8 strategy instance re-enable APIs are limited across environments. This watchdog stores snapshot state and exposes a controlled fallback path (`strategy_ui_restore.py`) that currently requests manual restore unless a UI automation backend is explicitly added.

