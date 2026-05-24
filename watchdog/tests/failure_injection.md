# Failure Injection Scenarios

These scenarios validate reconnect-first recovery, restart fallback, strategy activation retry behavior, and Telegram incident flow.

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
  - Watchdog waits for HealthBridge, dismisses benign dialogs, enables strategies, and stores a new snapshot only after strategy activation is verified.

## 4) Strategy Activation Retry Path
- Trigger: Let watchdog save a healthy snapshot with active strategies, then restart NT.
- Expected:
  - Watchdog calls `/strategies/enable_all`.
  - If activation is not verified, runtime state sets `strategy_enable_pending`.
  - Later healthy cycles retry strategy activation and do not overwrite the last-good snapshot until strategies are active.

## Automated Test Command
- `python -m unittest watchdog.tests.test_recovery -v`

