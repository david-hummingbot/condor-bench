"""Bench-tools MCP server for the A1 build driver (stdio, FastMCP).

Spawned by the agent bridge per session with env:

    BENCH_ROOT        condor-bench checkout
    BENCH_INSTANCE_ID instance under evaluation
    BENCH_RUN_DIR     where drafts/submission land

Runs under the condor-simple environment (imports the backtest engine
directly). Exposes exactly two tools — the build model's whole world:

- backtest_strategy: candidate-only backtest summaries (goldens are never
  read here, so the reference cannot leak into the build);
- submit_strategy: writes BENCH_RUN_DIR/submission.py (or DECLINE text)
  and ends the task from the driver's perspective.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

BENCH_ROOT = Path(os.environ["BENCH_ROOT"])
INSTANCE_ID = os.environ["BENCH_INSTANCE_ID"]
RUN_DIR = Path(os.environ["BENCH_RUN_DIR"])
MAX_BACKTESTS = int(os.environ.get("BENCH_MAX_BACKTESTS", "4"))

sys.path.insert(0, str(BENCH_ROOT / "harness"))

mcp = FastMCP("bench-tools")
_state = {"backtests": 0}


@mcp.tool()
def backtest_strategy(code: str) -> str:
    """Backtest a draft strategy file on this task's market-data fixtures.
    Returns per-fixture intent counts, sides, and close types for YOUR draft
    so you can verify behavior before submitting."""
    if _state["backtests"] >= MAX_BACKTESTS:
        return "backtest budget exhausted — submit your best draft now"
    _state["backtests"] += 1
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    draft = RUN_DIR / f"draft{_state['backtests']}.py"
    draft.write_text(code)
    try:
        from run_artifact import backtest_candidate

        intents = backtest_candidate(draft, INSTANCE_ID)
    except Exception as e:  # noqa: BLE001 — the model needs the error text
        return f"backtest error: {e}"
    return json.dumps(
        {
            fixture: {
                "n_intents": len(rows),
                "sides": sorted({r["side"] for r in rows}),
                "close_types": sorted({str(r["close_type"]) for r in rows}),
            }
            for fixture, rows in intents.items()
        }
    )


@mcp.tool()
def submit_strategy(code: str) -> str:
    """Finalize your strategy file — or, if the task cannot be faithfully
    expressed with the contract, submit 'DECLINE: <reason>'. Ends the task."""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    (RUN_DIR / "submission.py").write_text(code)
    return "submitted — you are done; do not continue working"


if __name__ == "__main__":
    mcp.run()
