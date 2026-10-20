"""Suite member worker — one process, one Condor checkout, one model × env.

Invoked by the suite orchestrator (never import condor in the API process for
live wiring of a different checkout).

Env / argv contract:
  CONDOR_PATH          — absolute path to the Condor checkout
  BENCH_SERVER_NAME    — optional override
  plus any model API keys the parent forwarded

  --job <path.json>    — job description written by the orchestrator
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path


def _load_job(path: Path) -> dict:
    return json.loads(path.read_text())


async def _run(job: dict) -> dict:
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from bench.baseline import BaselineStore
    from bench.cleanup import teardown
    from bench.client import run_case
    from bench.cleanup import created_resources
    from bench.dataset import is_mutating
    from bench.market_warmup import ensure_markets_for_case, warmup_failure_card
    from bench.probe_journal import ensure_probe_journal
    from bench.manifest import build_manifest
    from bench.mcp_provider import target_banner
    from bench.reporter import save_run
    from bench.scorer import score_case
    from bench.suites import load_suite_cases_as_objects, suite_prompt_map

    suite_id = job["suite_id"]
    environment_id = job["environment_id"]
    run_group_id = job["run_group_id"]
    model = job["model"]
    case_ids = job.get("case_ids")
    include_in_matrix = bool(job.get("include_in_matrix", False))
    member_run_id = job.get("member_run_id") or uuid.uuid4().hex[:8]

    from bench.staging_health import StagingUnhealthy, assert_ready

    try:
        assert_ready()
    except StagingUnhealthy as exc:
        return {"ok": False, "error": str(exc), "member_run_id": member_run_id}

    cases = load_suite_cases_as_objects(suite_id, case_ids=case_ids)
    if not cases:
        return {
            "ok": False,
            "error": "No suite cases matched (empty suite or unknown case ids)",
            "member_run_id": member_run_id,
        }

    # Pre-flight branch / clean checks from job expectations.
    from config import condor_checkout_state

    state = condor_checkout_state()
    expected_branch = job.get("expected_branch")
    if expected_branch and state.get("branch") != expected_branch:
        return {
            "ok": False,
            "error": (
                f"expected branch '{expected_branch}', got '{state.get('branch')}'"
            ),
            "member_run_id": member_run_id,
        }
    # Either side may be abbreviated: condor_checkout_state reports a short hash.
    expected_commit = str(job.get("expected_commit") or "")
    actual_commit = str(state.get("commit") or "")
    if expected_commit and not (
        actual_commit not in ("", "unknown")
        and (actual_commit.startswith(expected_commit) or expected_commit.startswith(actual_commit))
    ):
        return {
            "ok": False,
            "error": (
                f"expected commit '{expected_commit}', got '{state.get('commit')}'"
            ),
            "member_run_id": member_run_id,
        }
    if job.get("require_clean") and int(state.get("dirty_files") or 0) > 0:
        return {
            "ok": False,
            "error": f"checkout dirty ({state.get('dirty_files')} files)",
            "member_run_id": member_run_id,
        }

    store = BaselineStore()
    prompts = suite_prompt_map(suite_id)
    scorecards = []
    responses: dict[str, str] = {}

    from bench.reporter import trial_key
    from bench.scorer import with_trial

    trials = max(1, int(job.get("trials") or 1))
    for case in cases:
        ensure_probe_journal(case)
        warmup = await ensure_markets_for_case(case)
        if not warmup.ok:
            card = warmup_failure_card(case, model, warmup)
            scorecards.extend(with_trial(card, t) for t in range(1, trials + 1))
            continue
        for trial in range(1, trials + 1):
            result = await run_case(case, model)
            baseline = store.load(case.id)
            baseline_latency = baseline.latency_s if baseline else result.latency_s
            card = await score_case(case, result, baseline_latency, trial_idx=trial)
            scorecards.append(card)
            responses[trial_key(case.id, trial)] = result.response
            if is_mutating(case) or created_resources(result):
                await teardown(
                    result,
                    model,
                    agent_slug=getattr(case, "agent_slug", None),
                    tick=getattr(case, "type", "") == "tick",
                )

    pin = build_manifest(
        run_type="suite",
        suite_id=suite_id,
        environment_id=environment_id,
        run_group_id=run_group_id,
        case_ids=[c.id for c in cases],
        models=[model],
        include_in_matrix=include_in_matrix,
        shared_loaded=True,
        experiment_type_override=job.get("experiment_type"),
        trials=trials,
    )
    pin["target_banner"] = target_banner()
    pin["parent_run_id"] = job.get("parent_run_id")
    if job.get("preset"):
        pin["preset"] = job["preset"]

    run_dir = save_run(
        model,
        scorecards,
        responses,
        member_run_id,
        prompts=prompts,
        extra_summary=pin,
    )
    return {
        "ok": True,
        "member_run_id": member_run_id,
        "run_dir": run_dir.name,
        "summary_path": str(run_dir / "summary.json"),
        "cases": len(cases),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Suite member worker")
    parser.add_argument("--job", required=True, help="Path to job JSON")
    args = parser.parse_args()
    job = _load_job(Path(args.job))
    # Parent must set CONDOR_PATH; refuse to silently fall back to ../condor.
    if not os.environ.get("CONDOR_PATH") and not os.environ.get("CONDOR_REPO"):
        print(json.dumps({"ok": False, "error": "CONDOR_PATH not set for worker"}))
        return 2
    result = asyncio.run(_run(job))
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
