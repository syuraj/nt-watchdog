from __future__ import annotations

from typing import Any, Dict, List, Tuple


class StrategyUiRestorer:
    """
    Fallback strategy restorer.

    NT8 does not expose a consistently supported API for enabling strategy instances
    from AddOns in all environments, so this class provides a controlled fallback.
    The default implementation is intentionally conservative and returns a
    "manual-required" signal unless a UI automation backend is added.
    """

    def restore(self, snapshot: Dict[str, Any], current_runtime: Dict[str, Any]) -> Tuple[bool, str]:
        desired_runtime = snapshot.get("strategy_runtime", {})
        desired_rows: List[Dict[str, Any]] = desired_runtime.get("strategies", [])
        if not desired_rows:
            return True, "No strategies marked active in snapshot."

        desired_enabled = [row for row in desired_rows if bool(row.get("is_enabled"))]
        if not desired_enabled:
            return True, "Snapshot has zero enabled strategies."

        current_runtime_data = current_runtime.get("strategy_runtime", {})
        current_rows: List[Dict[str, Any]] = current_runtime_data.get("strategies", [])

        def row_key(row: Dict[str, Any]) -> str:
            return "|".join(
                [
                    str(row.get("account", "")),
                    str(row.get("name", "")),
                    str(row.get("instrument", "")),
                    str(row.get("template", "")),
                ]
            )

        current_enabled = {row_key(row) for row in current_rows if bool(row.get("is_enabled"))}
        missing = [row for row in desired_enabled if row_key(row) not in current_enabled]
        if not missing:
            return True, "Runtime already matches snapshot enabled strategy set."

        missing_names = ", ".join(
            sorted({f"{m.get('account','?')}:{m.get('name','?')}:{m.get('instrument','?')}" for m in missing})
        )
        # Safe default: do not perform blind GUI clicks without an explicit
        # automation backend tied to this VPS display/session.
        return False, f"Missing enabled strategies after restart. Manual enable required: {missing_names}"

