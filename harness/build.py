"""End-to-end A1 build evaluation: prompt -> LLM builds a strategy -> grade.

Drives a pinned model with Condor's REAL strategy-authoring skill
(strategy_backtest.md from the condor-simple checkout) and two tools:

- backtest_strategy(code): rehearse a draft on the instance's fixtures —
  returns ONLY the candidate's own backtest summary (never golden data, so
  the reference cannot leak into the build);
- submit_strategy(code): finalize the artifact.

After submission the artifact is backtested and graded deterministically
against the frozen goldens (harness/grade.py). Artifacts, transcripts, and
grades persist under results/builds/<instance>/<run>/ for pass@k analysis.

This is the "skill + contract fidelity" rung: it exercises Condor's skill
documentation and decide() contract with a pinned model, not yet the full
MCP product surface (agent-builder flow, manage_routines) — that driver is
the next rung and will reuse this grading tail.

Usage (condor-bench env; needs the model provider's API key):

    uv run python harness/build.py simple-rsi --model anthropic:claude-sonnet-4-6 --runs 3
    uv run python harness/build.py --all --runs 1
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

BENCH_ROOT = Path(__file__).resolve().parent.parent
CONDOR_REPO = Path(os.environ.get("CONDOR_REPO", Path.home() / "condor-simple"))
RESULTS_DIR = BENCH_ROOT / "results" / "builds"
DEFAULT_MODEL = "anthropic:claude-sonnet-4-6"
MAX_BACKTESTS_PER_BUILD = 4

sys.path.insert(0, str(BENCH_ROOT / "harness"))
from grade import grade_instance  # noqa: E402

SYSTEM_TEMPLATE = """You are Condor's strategy routine builder. Turn the user's
strategy description into ONE self-contained Python strategy file following the
contract below. Iterate with backtest_strategy if you want to verify behavior,
then finalize with submit_strategy. The file must define module-level PARAMS
(dict of defaults) and decide(candles, ctx) exactly per the contract.

The venue is Hyperliquid; available config classes are already importable:
from condor.backtest.types import Create, Stop
from condor.executors.position import PositionPerpConfig, PositionSpotConfig
from condor.executors.order import OrderPerpConfig, OrderSpotConfig

If the described strategy CANNOT be faithfully expressed with this contract
(e.g. it needs multiple coins' data, order-book/liquidation feeds, or another
venue), do NOT approximate silently: call submit_strategy with code="DECLINE: "
followed by one sentence naming the missing capability and your nearest honest
approximation.

=== Condor strategy contract (strategy_backtest.md) ===
{skill}
"""


def _run_backtest(strategy_code: str, instance_id: str, workdir: Path) -> dict:
    """Backtest a draft in the condor-simple env; returns summary-only info."""
    workdir.mkdir(parents=True, exist_ok=True)
    draft = workdir / "draft.py"
    draft.write_text(strategy_code)
    out_path = workdir / "draft_intents.json"
    proc = subprocess.run(
        [
            "uv", "run", "--project", str(CONDOR_REPO), "python",
            str(BENCH_ROOT / "harness" / "run_artifact.py"),
            str(draft), instance_id, "--out", str(out_path),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(BENCH_ROOT),
    )
    if proc.returncode != 0:
        return {"error": proc.stderr[-1500:]}
    intents = json.loads(out_path.read_text())
    # Candidate-only view: counts and close types, no golden data.
    return {
        fixture: {
            "n_intents": len(rows),
            "sides": sorted({r["side"] for r in rows}),
            "close_types": sorted({str(r["close_type"]) for r in rows}),
            "first_ts": rows[0]["created_ts"] if rows else None,
        }
        for fixture, rows in intents.items()
    }


async def build_one(instance_id: str, model: str, run_idx: int) -> dict:
    from pydantic_ai import Agent

    inst_dir = BENCH_ROOT / "datasets" / "instances" / instance_id
    instance = json.loads((inst_dir / "instance.json").read_text())
    prompt = (inst_dir / "prompt.md").read_text()
    skill = (
        CONDOR_REPO / "skills" / "routine-builder" / "strategy_backtest.md"
    ).read_text()

    run_dir = RESULTS_DIR / instance_id / f"run{run_idx}"
    run_dir.mkdir(parents=True, exist_ok=True)
    state = {"submitted": None, "backtests": 0}

    agent = Agent(model, system_prompt=SYSTEM_TEMPLATE.format(skill=skill))

    @agent.tool_plain
    def backtest_strategy(code: str) -> str:
        """Backtest a draft strategy file on this task's market-data fixtures.
        Returns per-fixture intent counts and close types for your own draft."""
        if state["backtests"] >= MAX_BACKTESTS_PER_BUILD:
            return "backtest budget exhausted — submit your best draft"
        state["backtests"] += 1
        return json.dumps(_run_backtest(code, instance_id, run_dir))

    @agent.tool_plain
    def submit_strategy(code: str) -> str:
        """Finalize the strategy file (or a DECLINE: message). Ends the task."""
        state["submitted"] = code
        return "submitted"

    result = await agent.run(prompt)
    (run_dir / "transcript.json").write_text(
        json.dumps([m.__class__.__name__ for m in result.all_messages()], indent=2)
    )

    record: dict = {
        "instance": instance_id,
        "model": model,
        "run": run_idx,
        "feasibility": instance["feasibility"],
        "usage": repr(result.usage()),
    }
    submitted = state["submitted"]
    if submitted is None:
        record.update(outcome="no_submission", resolved=False)
    elif submitted.strip().startswith("DECLINE:"):
        (run_dir / "decline.txt").write_text(submitted)
        correct = instance["feasibility"] == "needs-decline"
        record.update(
            outcome="declined", decline_text=submitted[:500], resolved=correct
        )
    elif instance["feasibility"] == "needs-decline":
        (run_dir / "strategy.py").write_text(submitted)
        record.update(outcome="built_when_should_decline", resolved=False)
    else:
        artifact = run_dir / "strategy.py"
        artifact.write_text(submitted)
        bt = _run_backtest(submitted, instance_id, run_dir)
        if "error" in bt:
            record.update(outcome="artifact_error", error=bt["error"], resolved=False)
        else:
            intents = json.loads((run_dir / "draft_intents.json").read_text())
            grade = grade_instance(
                inst_dir, intents, BENCH_ROOT / "datasets" / "fixtures"
            )
            record.update(outcome="graded", grade=grade, resolved=grade["resolved"])
    (run_dir / "result.json").write_text(json.dumps(record, indent=2, default=str))
    return record


async def amain(args) -> int:
    instances_dir = BENCH_ROOT / "datasets" / "instances"
    if args.all:
        ids = sorted(
            d.name for d in instances_dir.iterdir() if (d / "instance.json").exists()
        )
    else:
        ids = [args.instance]
    records = []
    for iid in ids:
        for run_idx in range(args.runs):
            print(f"building {iid} run {run_idx} with {args.model} ...")
            rec = await build_one(iid, args.model, run_idx)
            print(f"  -> {rec['outcome']} resolved={rec['resolved']}")
            records.append(rec)

    by_instance: dict[str, list] = {}
    for r in records:
        by_instance.setdefault(r["instance"], []).append(r)
    print("\n== scorecard ==")
    n_pass1 = 0
    for iid, rs in sorted(by_instance.items()):
        k = sum(1 for r in rs if r["resolved"])
        n_pass1 += rs[0]["resolved"]
        print(f"  {iid:<26} pass {k}/{len(rs)}")
    print(f"pass@1: {n_pass1}/{len(by_instance)}")
    return 0


def main() -> int:
    import asyncio

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("instance", nargs="?", help="instance id")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--runs", type=int, default=1)
    args = ap.parse_args()
    if not args.all and not args.instance:
        ap.error("pass an instance id or --all")
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
