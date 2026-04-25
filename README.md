# NT Watchdog

Self-healing health monitor for NinjaTrader 8 on a Windows VPS.

Two parts:
- `HealthBridge.cs`: NT8 AddOn exposing local HTTP health/recovery endpoints on port `8899`.
- `watchdog/`: Python watchdog that polls the bridge, runs staged recovery, and sends Telegram alerts.

## What It Does
- Detects NT health issues (`/healthz`) such as stale UI/main-thread, connection instability, and blocking windows.
- Uses NT connection state only for health/recovery decisions (no external internet probe dependency).
- Runs health checks once per minute by default (`poll_interval_sec: 60`) to reduce log noise.
- Attempts staged recovery (reconnect first, restart fallback with circuit breaker).
- For `no_connections_detected`, attempts reconnect-only recovery with a dedicated cooldown (`no_connections_recovery_cooldown_sec`) and does not escalate to NT process restart from that reason alone.
- Supports configured reconnect targets via `connection_names` in `config.yaml` (for example `My NinjaTrader`).
- Saves last-known-good runtime snapshot for restore workflows.
- Open-position policy: flatten then recover.
- Deduplicates repeat incident alerts during cooldown (`notification_cooldown_sec`).

## Quick Setup (Python-first)
1. From repo root, run setup (creates venv, installs deps, copies `HealthBridge.cs` to NT8 AddOns folder):
   - `python scripts/manage_watchdog.py setup`
   - Optional params to override `--bridge-url http://localhost:8899` or `--nt-executable-path "C:\Your\Path\NinjaTrader.exe"`
2. In NinjaTrader: NinjaScript → Compile (F5) so `HealthBridge` loads.
3. Install RDP disconnect handler (elevated shell) — prevents chart freeze on disconnect:
   - `python scripts/manage_watchdog.py install-rdp-handler`
4. (Optional) Configure Telegram alerts — edit `config.yaml`:
   ```yaml
   telegram_bot_token: "<token>"
   telegram_chat_id: "<chat_id>"
   telegram_allowed_user_ids: "<user_ids>"
   ```
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

## Startup On Login (Optional)
- `python scripts/manage_watchdog.py install-startup`
- remove with `python scripts/manage_watchdog.py remove-startup`
- trigger startup now (without relogin):
  - `python scripts/manage_watchdog.py trigger-startup`

## RDP Disconnect Handler
Scheduled task triggered on TerminalServices Event ID 24 runs `tscon /dest:console` to re-attach the disconnected session, keeping WPF/Direct3D rendering alive. Install via step 3 above. Remove with `python scripts/manage_watchdog.py remove-rdp-handler`. Log: `watchdog/logs/rdp_handler.log`.

## Logs and State
- Runtime events: `watchdog/logs/health_events.jsonl`
- Last good snapshot: `watchdog/state/last_good_snapshot.json`
- Runtime counters: `watchdog/state/runtime_state.json`

## Notes
- NT8 strategy re-enable APIs are limited. Watchdog uses UI automation to toggle strategy checkboxes on the Strategies tab after reconnect; falls back to manual restore request if UIA fails.
- See `AGENTS.md` for architecture, editing rules, and verification checklist.

