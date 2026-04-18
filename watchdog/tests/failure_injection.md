# Failure Injection Scenarios

These scenarios validate reconnect-first recovery, restart fallback, snapshot restore behavior, and Telegram incident flow.

## 1) Connection Drop (Recoverable)
- Trigger: Disconnect broker/data feed from NT8 manually.
- Expected:
  - `/healthz` becomes `degraded` with connection reasons.
  - Watchdog calls `/recover/reconnect` (or `/recover/flatten_then_reconnect` when open positions exist).
  - If reconnect succeeds, no restart occurs.
  - Telegram receives `reconnect_success`.

## 2) Simulated NT Freeze / Stuck Bridge
- Trigger: Force NT to stop responding (e.g., suspend process briefly) so `/healthz` times out or reports `stuck`.
- Expected:
  - Reconnect attempt fails repeatedly.
  - Watchdog reaches reconnect attempt limit, then restarts NT.
  - Restart count is tracked and capped by circuit breaker.
  - Telegram receives `restart_success` or `restart_failed`.

## 3) Process Crash
- Trigger: Kill `NinjaTrader.exe`.
- Expected:
  - Watchdog detects process not running and starts NT from configured fixed path.
  - During startup grace, watchdog avoids aggressive recovery loops.
  - Once healthy, watchdog stores new snapshot.

## 4) Snapshot Restore Path
- Trigger: Let watchdog save a healthy snapshot with active strategies, then restart NT.
- Expected:
  - Runtime state sets `awaiting_restore`.
  - On healthy cycle, restore workflow compares current vs snapshot.
  - If backend restore is unavailable, Telegram emits `restore_manual_required` with missing strategies list.

## Automated Test Command
- `python -m unittest watchdog.tests.test_recovery -v`

