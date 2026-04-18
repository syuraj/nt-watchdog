# AGENTS.md

Guidance for future coding agents working in this repository.

## Project Purpose
- Build and maintain an NT8 self-healing stack for a Windows VPS.
- Components:
  - `HealthBridge.cs` (NinjaTrader AddOn, in-process HTTP bridge)
  - `watchdog/` (Python external monitor/recovery + Telegram alerts)

## Current Runtime Model
- Bridge endpoint base: `http://localhost:8899`
- Core bridge endpoints:
  - `/health`, `/healthz`, `/runtime_snapshot`
  - `/recover/reconnect`, `/recover/flatten_then_reconnect`
- Watchdog decisions are based on **NT/HealthBridge connection state only** (no internet probing).

## Important Commands
- Setup:
  - `python scripts/manage_watchdog.py setup --bridge-url http://localhost:8899 --nt-executable-path "C:\Path\To\NinjaTrader.exe"`
- Run watchdog:
  - `python scripts/manage_watchdog.py run`
- Status:
  - `python scripts/manage_watchdog.py status`
  - `python scripts/manage_watchdog.py status --verbose`
- Startup on login (no admin):
  - `python scripts/manage_watchdog.py install-startup`
  - `python scripts/manage_watchdog.py trigger-startup`
  - `python scripts/manage_watchdog.py remove-startup`

## Logs and State
- Watchdog event log: `watchdog/logs/health_events.jsonl`
- Snapshot: `watchdog/state/last_good_snapshot.json`
- Runtime state: `watchdog/state/runtime_state.json`
- NT logs: `C:\Users\sshrestha\Documents\NinjaTrader 8\log\log.*.txt`
- Bridge runtime messages go to NT Output window (`[HealthBridge] ...`).

## Editing Rules
- Prefer Python over PowerShell for new automation logic.
- Keep `scripts/manage_watchdog.py` as the main operational CLI.
- PowerShell scripts in `scripts/` are wrappers only.
- Do not reintroduce Task Scheduler logic unless explicitly requested.
- Keep status output concise by default; details behind `--verbose`/`--json`.
- Ensure alert send failures are visible in both terminal output and event logs.

## Verification Checklist
- Python checks:
  - `python -m compileall scripts watchdog`
  - `python -m unittest watchdog.tests.test_recovery watchdog.tests.test_telegram_notifier -v`
- Bridge compile check (when NT + old bridge on 8888 available):
  - `POST http://localhost:8888/compile`
- Live bridge checks:
  - `GET http://localhost:8899/health`
  - `GET http://localhost:8899/healthz`

