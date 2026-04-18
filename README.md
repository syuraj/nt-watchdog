# NT8 Health Watchdog

This project adds a self-healing health monitor for NinjaTrader 8 on a Windows VPS.

It has two parts:
- `HealthBridge.cs`: an NT8 AddOn that exposes local HTTP health/recovery endpoints.
- `watchdog/`: a Python watchdog that monitors those endpoints, attempts recovery, and sends Telegram alerts.

Default bridge port is `8899` (to avoid conflicts with other tools on `8888`).

## What It Does
- Detects NT health issues (`/healthz`) such as stale UI/main-thread, connection instability, and blocking windows.
- Uses NT connection state only for health/recovery decisions (no external internet probe dependency).
- Runs health checks once per minute by default (`poll_interval_sec: 60`) to reduce log noise.
- Attempts staged recovery (reconnect first, restart fallback with circuit breaker).
- For `no_connections_detected`, attempts reconnect-only recovery with a dedicated cooldown (`no_connections_recovery_cooldown_sec`) and does not escalate to NT process restart from that reason alone.
- Supports configured reconnect targets via `connection_names` in `watchdog/config.yaml` (for example `My NinjaTrader`).
- Saves last-known-good runtime snapshot for restore workflows.
- Logs watchdog events to `watchdog/logs/health_events.jsonl`.

## Quick Setup (Python-first)
1. Copy bridge source to NT8 AddOns folder:
   - `C:\Users\<you>\Documents\NinjaTrader 8\bin\Custom\AddOns\HealthBridge.cs`
2. In NinjaTrader, compile NinjaScript so `HealthBridge` loads.
3. From this repo root, run:
   - `python scripts/manage_watchdog.py setup --bridge-url http://localhost:8899 --nt-executable-path "C:\Path\To\NinjaTrader.exe"`
4. (Optional) Configure Telegram env vars:
   - `setx TELEGRAM_BOT_TOKEN "<token>"`
   - `setx TELEGRAM_CHAT_ID "<chat_id>"`
5. Start watchdog:
   - `python scripts/manage_watchdog.py run`

Optional env overrides:
- `setx WATCHDOG_NO_CONNECTIONS_RECOVERY_COOLDOWN_SEC "300"`
- `setx WATCHDOG_NOTIFICATION_COOLDOWN_SEC "900"`

## Verify
- Bridge liveness:
  - `http://localhost:8899/health`
- Detailed health:
  - `http://localhost:8899/healthz`
- One-shot combined status:
  - `python scripts/manage_watchdog.py status`
- JSON status:
  - `python scripts/manage_watchdog.py status --json`
- Mock smoke test:
  - `python scripts/manage_watchdog.py smoke-test --cleanup`

## Startup On Login
- `python scripts/manage_watchdog.py install-startup`
- remove with `python scripts/manage_watchdog.py remove-startup`
- trigger startup now (without relogin):
  - `python scripts/manage_watchdog.py trigger-startup`

For watchdog internals and test commands, see `watchdog/README.md`.

