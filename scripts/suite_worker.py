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
import signal
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
    from bench.manifest import build_manifest
    from bench.mcp_provider import target_banner
    from bench.reporter import save_run
    from bench.suites import load_suite_cases_as_objects, suite_prompt_map

    suite_id = job["suite_id"]
    environment_id = job["environment_id"]
    run_group_id = job["run_group_id"]
    model = job["model"]
    case_ids = job.get("case_ids")
    include_in_matrix = bool(job.get("include_in_matrix", False))
    member_run_id = job.get("member_run_id") or uuid.uuid4().hex[:8]

    from bench.staging_health import StagingUnhealthy, a_assert_ready

    # The async form: this already runs inside asyncio.run(), and the sync
    # assert_ready() starts its own loop — which failed every suite member at start.
    try:
        await a_assert_ready()
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

    # The shared trial runner (bench.trials): the same seeding, timeouts, crash
    # rows and teardown a CLI or dashboard run gets. This worker used to carry its
    # own loop with no per-case timeout, so one hung case stalled a whole member.
    from bench.trials import cases_for_lane, run_cases

    trials = max(1, int(job.get("trials") or 1))
    cases, _skipped = cases_for_lane(cases)

    # Bind declared markets ({venue.pair} …) against this checkout's target. A case
    # that cannot bind is recorded as a harness row per trial, not run with its
    # placeholders and not silently dropped.
    from bench.market_resolver import resolve_cases, split_resolvable, unresolvable_card
    from bench.scorer import with_trial

    bound, resolutions = await resolve_cases(cases)
    cases, unbindable = split_resolvable(bound, resolutions)
    for case in unbindable:
        card = unresolvable_card(case, model, resolutions[case.id])
        scorecards.extend(with_trial(card, t) for t in range(1, trials + 1))
    for case in cases:
        rendered = getattr(case, "question", None) or getattr(case, "scenario_name", None)
        if rendered:
            prompts[case.id] = rendered
    await run_cases(
        cases, model, store, trials, scorecards=scorecards, responses=responses,
        events=_progress_events(cases, model, trials, already=len(scorecards)),
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


def _progress_events(cases: list, model: str, trials: int, *, already: int):
    """Report each trial on stdout as it starts and finishes (``PROGRESS_PREFIX``
    lines), so the dashboard can show a member's progress while it runs."""
    from bench.client import case_input_text
    from bench.suite_runner import PROGRESS_PREFIX
    from bench.trials import TrialEvents

    total = already + len(cases) * trials
    done = already

    def send(event: dict) -> None:
        print(PROGRESS_PREFIX + json.dumps(event, default=str), flush=True)

    async def trial_started(case, trial, n):
        send({"type": "case_started", "model": model, "case_id": case.id, "trial": trial,
              "trials": n, "case_type": case.type, "case_number": done + 1, "total": total,
              "question": case_input_text(case)})

    async def trial_done(case, trial, card, response, error):
        nonlocal done
        done += 1
        scorecard = card.as_dict()
        scorecard["question"] = case_input_text(case)
        send({"type": "case_done", "model": model, "case_id": case.id, "trial": trial,
              "scorecard": scorecard, "response": (response or "")[:1000],
              "question": scorecard["question"], "error": error,
              "completed": done, "total": total})

    return TrialEvents(trial_started=trial_started, trial_done=trial_done)


async def _run_until_stopped(job: dict) -> dict:
    """Run the job; SIGTERM from the suite runner cancels it instead of killing it.

    Cancelling is what lets the trial in flight undo what it already created
    (bench.trials tears down on CancelledError) — a plain SIGTERM would exit with
    an order still open on staging.
    """
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()

    def stop_once() -> None:
        # One cancel only. The runner signals the whole process group and `uv run`
        # forwards its own SIGTERM too, so the worker is signalled twice — and a
        # second task.cancel() aborted the teardown the first one started. Later
        # SIGTERMs are ignored; the runner's SIGKILL after its grace still stops it.
        loop.add_signal_handler(signal.SIGTERM, lambda: None)
        task.cancel()

    loop.add_signal_handler(signal.SIGTERM, stop_once)
    try:
        return await _run(job)
    except asyncio.CancelledError:
        return {"ok": False, "error": "cancelled", "member_run_id": job.get("member_run_id")}


def main() -> int:
    parser = argparse.ArgumentParser(description="Suite member worker")
    parser.add_argument("--job", required=True, help="Path to job JSON")
    args = parser.parse_args()
    job = _load_job(Path(args.job))
    # Parent must set CONDOR_PATH; refuse to silently fall back to ../condor.
    if not os.environ.get("CONDOR_PATH") and not os.environ.get("CONDOR_REPO"):
        print(json.dumps({"ok": False, "error": "CONDOR_PATH not set for worker"}))
        return 2
    result = asyncio.run(_run_until_stopped(job))
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
