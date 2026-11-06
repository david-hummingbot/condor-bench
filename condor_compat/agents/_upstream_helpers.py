"""GENERATED — do not edit. Vendored from condor by scripts/revendor.py.

Source: condor/agents/{config,quote_render,journal}.py (helpers only)
condor:  a817f663-dirty (main)

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


# ── from condor/agents/quote_render.py ──
#: Quote assets a ``$`` renders correctly. Dollar stablecoins trade close
#: enough to the dollar that "$500" is the figure an operator wants to see, and
#: this is what every strategy on a CEX venue quotes in — so the overwhelmingly
#: common case keeps exactly the rendering it always had.
DOLLAR_QUOTES = frozenset({"USD", "USDT", "USDC", "DAI", "BUSD", "FDUSD"})

#: Decimals for each form. A dollar figure wants cents; a token figure wants
#: more, because the amounts are small — an LP slot is a fraction of one SOL,
#: and ``0.03 SOL`` rendered to two places is ``0.03``, which reads as a
#: rounding of anything between 0.025 and 0.035.
_DOLLAR_DECIMALS = 2

_TOKEN_DECIMALS = 4

def normalize_quote(quote_asset: Any) -> str:
    """The quote symbol, upper-cased and stripped, or ``""``.

    ``""`` is the "caller did not say" case and renders as dollars, which is
    both the previous behaviour and the right answer for the venues that do not
    set it.
    """
    return str(quote_asset or "").strip().upper()

def is_dollar_quote(quote_asset: Any) -> bool:
    """Whether figures in ``quote_asset`` should carry a ``$``."""
    unit = normalize_quote(quote_asset)
    return not unit or unit in DOLLAR_QUOTES

def format_quote(value: Any, quote_asset: Any = "") -> str:
    """``$500.00`` for a dollar quote, ``0.5000 SOL`` for anything else.

    A value that is not a number renders as ``0`` in the appropriate form rather
    than raising: every caller is building a message — a prompt line or a
    refusal reason — and a message that cannot be built is worse than one
    carrying a zero, especially on the refusal path, where raising here would
    escape the permission callback and skip the ``RefusalLog`` entirely.
    """
    try:
        amount = float(value or 0.0)
    except (TypeError, ValueError):
        amount = 0.0
    unit = normalize_quote(quote_asset)
    if is_dollar_quote(unit):
        return f"${amount:,.{_DOLLAR_DECIMALS}f}"
    return f"{amount:,.{_TOKEN_DECIMALS}f} {unit}"


# ── from condor/agents/journal.py ──
def render_risk_lines(risk_state: dict[str, Any], bullet: str = "- ") -> list[str]:
    """The risk block, one line per limit, shared by the tick prompt and snapshots.

    Every value keeps a ``.get`` default: a failed dry run records
    ``risk_state={}`` and must still render.
    """
    rs = risk_state
    # What the gate's figures are actually denominated in (LP-2), shared with
    # the gate's own refusals so the two cannot disagree about a limit they are
    # both describing (``condor.agents.quote_render``).
    unit = rs.get("quote_asset") or ""

    def _amount(value: float) -> str:
        return format_quote(value, unit)

    max_dd = rs.get("max_drawdown_pct", -1)
    if max_dd < 0:
        dd_display = "disabled"
    elif not rs.get("drawdown_known", True):
        # A tick can now RUN with no drawdown on record: before its first
        # deploy there is nothing to have fallen from, and a tick whose own
        # providers answered is not flying blind even though the journal has
        # not been written yet ([[CORR-750]]). Both used to block, so the
        # "BLOCKED" line said as much; now the figure has to say it itself, or
        # the agent reads a measured 0.0% and plans against a floor nobody has
        # measured.
        dd_display = f"not measured yet (no reading on record) / {max_dd:.1f}% limit"
    else:
        dd_display = f"{rs.get('drawdown_pct', 0):.1f}% / {max_dd:.1f}% limit"
    lines = [
        f"Position Size: {_amount(rs.get('total_exposure', 0))} / "
        f"{_amount(rs.get('max_position_size', 500))} limit",
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
    # A limit the agent is NOT told about is a limit it will trip; a limit it is
    # wrongly told it has is one it will rely on. ``min_wallet_sol_reserve`` and
    # ``max_open_slots`` sat in lp_slot_operator's risk_limits bounding nothing,
    # and the loop planned its slots around them (LP-3). Named here, in the same
    # block as the real limits, because that is where the agent looks.
    unrecognized = rs.get("unrecognized_limits") or []
    if unrecognized:
        lines.append(
            f"NOT ENFORCED: {', '.join(unrecognized)} — set in this strategy's "
            "risk_limits but unknown to the risk engine, so they bound nothing. "
            "Do not plan around them."
        )
    # "Risk Check", not "Status": a snapshot outlives its session, and a stopped
    # run's history reading "Status: ACTIVE" looked like the run was still live.
    # This is the risk gate's verdict for this one tick, nothing more.
    lines.append(
        f"Risk Check: {'BLOCKED - ' + rs.get('block_reason', '') if rs.get('is_blocked') else 'passed'}"
    )
    return [bullet + line for line in lines]
