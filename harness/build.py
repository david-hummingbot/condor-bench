"""End-to-end A1 build evaluation: prompt -> agent builds a strategy -> grade.

Reuses condor-simple's agent-client factory (`condor.acp.client.
build_agent_client`), so the builder model runs on ANY backend Condor
supports with one flag:

- `claude-acp:<tier>`  — the operator's Claude SUBSCRIPTION via the
  claude-agent-acp bridge (no API key; the default);
- `<provider>:<model>` — API-key providers (openrouter/kimi/deepseek/...)
  via Condor's direct runner.

The model gets Condor's real strategy_backtest.md skill in the prompt and a
dedicated bench-tools MCP server exposing exactly two tools:
backtest_strategy (candidate-only summaries — goldens never enter the build
context) and submit_strategy (accepts `DECLINE: <reason>` for infeasible
tasks). The permission callback DENIES everything else — claude-agent-acp is
full Claude Code, and without the denylist the model could read goldens off
disk. The working dir is the empty run dir for the same reason.

After submission the artifact is backtested and graded deterministically
(harness/grade.py); every run persists under results/builds/<instance>/<run>/
for pass@k analysis.

Run under the condor-simple environment:

    make build-eval INSTANCE=simple-rsi              # subscription, sonnet
    make build-eval INSTANCE=simple-rsi MODEL=claude-acp:haiku RUNS=3
    make build-eval-all MODEL=openrouter:openai/gpt-4o-mini
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

BENCH_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = BENCH_ROOT / "results" / "builds"
DEFAULT_MODEL = "claude-acp:sonnet"
PROMPT_TIMEOUT_S = 900

sys.path.insert(0, str(BENCH_ROOT / "harness"))
from grade import grade_instance  # noqa: E402
from run_artifact import backtest_candidate  # noqa: E402

try:
    from condor.acp.client import build_agent_client
except ImportError as e:  # noqa: F841
    sys.exit(
        "build.py must run under the condor-simple environment:\n"
        "  make build-eval INSTANCE=<id>   (or: uv run --project ~/condor-simple "
        "python harness/build.py <id>)"
    )

CONDOR_REPO = Path(os.environ.get("CONDOR_REPO", Path.home() / "condor-simple"))

TASK_TEMPLATE = """You are Condor's strategy routine builder. Turn the strategy
description below into ONE self-contained Python strategy file per the contract.
Use ONLY the bench-tools MCP tools: iterate with backtest_strategy if useful,
then finalize with submit_strategy — the submitted code is your entire answer.
Do not read or write files, run shell commands, or use any other tool.

The venue is Hyperliquid; importable config classes:
from condor.backtest.types import Create, Stop
from condor.executors.position import PositionPerpConfig, PositionSpotConfig
from condor.executors.order import OrderPerpConfig, OrderSpotConfig

If the description CANNOT be faithfully expressed with this contract (e.g. it
needs multiple coins' data, order-book/liquidation feeds, or another venue),
do NOT approximate silently: submit_strategy with code "DECLINE: <one sentence
naming the missing capability and your nearest honest approximation>".

=== Condor strategy contract (strategy_backtest.md) ===
{skill}

=== The user's strategy description ===
{prompt}
"""


def _bench_mcp_config(instance_id: str, run_dir: Path) -> dict:
    return {
        "name": "bench-tools",
        "command": sys.executable,
        "args": [str(BENCH_ROOT / "harness" / "bench_tools_server.py")],
        "env": [
            {"name": "BENCH_ROOT", "value": str(BENCH_ROOT)},
            {"name": "BENCH_INSTANCE_ID", "value": instance_id},
            {"name": "BENCH_RUN_DIR", "value": str(run_dir)},
        ],
    }


async def _deny_all_but_bench_tools(tool_call: dict, options: list[dict]) -> dict:
    """Permission gate: only the bench-tools MCP tools are approved."""
    blob = json.dumps(tool_call)
    allowed = any(
        marker in blob
        for marker in ("bench-tools", "bench_tools", "backtest_strategy", "submit_strategy")
    )
    wanted = ("allow_once", "allow_always") if allowed else ("reject_once", "reject_always")
    for opt in options:
        if opt.get("kind") in wanted:
            return {"outcome": {"outcome": "selected", "optionId": opt["optionId"]}}
    return {"outcome": {"outcome": "cancelled"}}


async def build_one(instance_id: str, model: str, run_idx: int) -> dict:
    inst_dir = BENCH_ROOT / "datasets" / "instances" / instance_id
    instance = json.loads((inst_dir / "instance.json").read_text())
    skill = (
        CONDOR_REPO / "skills" / "routine-builder" / "strategy_backtest.md"
    ).read_text()
    task = TASK_TEMPLATE.format(skill=skill, prompt=(inst_dir / "prompt.md").read_text())

    run_dir = RESULTS_DIR / instance_id / f"run{run_idx}"
    run_dir.mkdir(parents=True, exist_ok=True)

    client = build_agent_client(
        model,
        working_dir=str(run_dir),  # empty dir: nothing to read but the task
        mcp_servers=[_bench_mcp_config(instance_id, run_dir)],
        permission_callback=_deny_all_but_bench_tools,
    )
    record: dict = {
        "instance": instance_id,
        "model": model,
        "run": run_idx,
        "feasibility": instance["feasibility"],
    }
    try:
        await client.start()
        final_text = await asyncio.wait_for(client.prompt(task), timeout=PROMPT_TIMEOUT_S)
        record["usage"] = client.last_usage
        record["active_model_id"] = getattr(client, "active_model_id", None)
        (run_dir / "final_message.md").write_text(final_text or "")
    except asyncio.TimeoutError:
        record.update(outcome="timeout", resolved=False)
        return _finish(run_dir, record)
    except Exception as e:  # noqa: BLE001
        record.update(outcome="client_error", error=str(e)[:1500], resolved=False)
        return _finish(run_dir, record)
    finally:
        try:
            await client.stop()
        except Exception:  # noqa: BLE001
            pass

    submission_path = run_dir / "submission.py"
    if not submission_path.exists():
        record.update(outcome="no_submission", resolved=False)
        return _finish(run_dir, record)
    submitted = submission_path.read_text()

    if submitted.strip().startswith("DECLINE:"):
        correct = instance["feasibility"] == "needs-decline"
        record.update(outcome="declined", decline_text=submitted[:500], resolved=correct)
    elif instance["feasibility"] == "needs-decline":
        record.update(outcome="built_when_should_decline", resolved=False)
    else:
        try:
            intents = backtest_candidate(submission_path, instance_id)
        except Exception as e:  # noqa: BLE001
            record.update(outcome="artifact_error", error=str(e)[:1500], resolved=False)
            return _finish(run_dir, record)
        grade = grade_instance(inst_dir, intents, BENCH_ROOT / "datasets" / "fixtures")
        record.update(outcome="graded", grade=grade, resolved=grade["resolved"])
    return _finish(run_dir, record)


def _finish(run_dir: Path, record: dict) -> dict:
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
            print(f"  -> {rec.get('outcome')} resolved={rec.get('resolved')}")
            records.append(rec)

    by_instance: dict[str, list] = {}
    for r in records:
        by_instance.setdefault(r["instance"], []).append(r)
    print("\n== scorecard ==")
    n_pass1 = 0
    for iid, rs in sorted(by_instance.items()):
        k = sum(1 for r in rs if r.get("resolved"))
        n_pass1 += bool(rs[0].get("resolved"))
        print(f"  {iid:<26} pass {k}/{len(rs)}")
    print(f"pass@1: {n_pass1}/{len(by_instance)}")
    return 0


def main() -> int:
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
