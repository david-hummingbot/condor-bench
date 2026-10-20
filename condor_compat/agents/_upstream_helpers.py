"""GENERATED — do not edit. Vendored from condor by scripts/revendor.py.

Source: condor/agents/{config,journal}.py (helpers only)
condor:  8e4860fa-dirty (main)

Imports are rewritten to condor_compat/upstream_shims.py; the prompt text itself
is byte-for-byte condor's. Bench's additions are appended below the marker at the
end of this file — edit condor_compat/agents/_bench_prompts.py for those.

Re-pull with:  uv run python scripts/revendor.py
"""
from __future__ import annotations

from typing import Any


# ── from condor/agents/config.py ──
# The execution modes that run a single tick as an experiment: no journal, the
# tick captured as a dry-run snapshot instead. The one definition of that set —
# AgentConfig.execution_mode below is the Literal naming every mode.
EXPERIMENT_MODES: frozenset[str] = frozenset({"dry_run", "run_once"})

def is_experiment_mode(mode: str) -> bool:
    """True for an execution mode that runs as an experiment (dry_run/run_once)."""
    return mode in EXPERIMENT_MODES


# ── from condor/agents/journal.py ──
def render_risk_lines(risk_state: dict[str, Any], bullet: str = "- ") -> list[str]:
    """The risk block, one line per limit, shared by the tick prompt and snapshots.

    Every value keeps a ``.get`` default: a failed dry run records
    ``risk_state={}`` and must still render.
    """
    rs = risk_state
    max_dd = rs.get("max_drawdown_pct", -1)
    dd_display = (
        f"{rs.get('drawdown_pct', 0):.1f}% / {max_dd:.1f}% limit"
        if max_dd >= 0
        else "disabled"
    )
    lines = [
        f"Position Size: ${rs.get('total_exposure', 0):.2f} / ${rs.get('max_position_size', 500):.2f} limit",
        f"Open Executors: {rs.get('executor_count', 0)} / {rs.get('max_open_executors', 5)} limit",
        f"Drawdown: {dd_display}",
    ]
    # Only when one is set: a leverage limit is off by default ([[SEC-558]]),
    # and a line reading "disabled" invites the agent to go looking for the
    # ceiling. When it IS set, it has to be here — a limit the agent is not
    # told about is a limit it will trip, and every create it makes on a perp
    # has to declare a leverage at or under it.
    max_leverage = rs.get("max_leverage", -1)
    if max_leverage >= 0:
        lines.append(
            f"Max Leverage: {max_leverage:g}x "
            "(declare `leverage` on every create; omitting it is refused)"
        )
    # "Risk Check", not "Status": a snapshot outlives its session, and a stopped
    # run's history reading "Status: ACTIVE" looked like the run was still live.
    # This is the risk gate's verdict for this one tick, nothing more.
    lines.append(
        f"Risk Check: {'BLOCKED - ' + rs.get('block_reason', '') if rs.get('is_blocked') else 'passed'}"
    )
    return [bullet + line for line in lines]
