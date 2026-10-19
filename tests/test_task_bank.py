"""Phase 4: how the task bank reaches a run — splits, lanes, teardown, reporting."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
import typer

from bench.dataset import load_all_cases, load_holdout_cases
from bench.scorer import ScoreCard


def test_holdout_needs_release(monkeypatch):
    import runner

    with pytest.raises(typer.Exit):
        runner._resolve_splits("holdout", release=False)
    assert runner._resolve_splits(None, release=False) is None
    assert runner._resolve_splits("regression", release=True) == ["regression", "holdout"]
    with pytest.raises(typer.Exit):
        runner._resolve_splits("nonsense", release=False)


def test_release_loads_the_holdout_split():
    held = {c.id for c in load_holdout_cases()}
    assert held <= {c.id for c in load_all_cases(include_holdout=True)}


def test_mock_only_cases_are_left_out_of_live_runs(monkeypatch):
    import runner

    cases = [c for c in load_all_cases() if c.id in ("c_safety_insufficient_001", "c002")]
    monkeypatch.delenv("BENCH_ENV_MODE", raising=False)
    assert [c.id for c in runner._cases_for_lane(cases)] == ["c002"]
    monkeypatch.setenv("BENCH_ENV_MODE", "mock")
    assert {c.id for c in runner._cases_for_lane(cases)} == {"c002", "c_safety_insufficient_001"}


def test_a_read_only_case_that_created_something_is_torn_down(monkeypatch):
    """The prompt-injection case is read-only; a model that falls for it still made an executor."""
    import bench.cleanup
    import bench.client
    import bench.scorer
    import runner

    monkeypatch.delenv("BENCH_ENV_MODE", raising=False)
    torn: list = []

    async def fake_run_case(case, model):
        return SimpleNamespace(latency_s=1.0, response="done")

    async def fake_score_case(case, result, baseline, *, trial_idx=1):
        return ScoreCard(case_id=case.id, model=model_key, answer_quality=1.0, answer_reason="",
                         tool_accuracy=0.0, latency_score=1.0, composite=0.0, latency_s=1.0,
                         baseline_latency_s=1.0)

    async def fake_teardown(result, model, **kw):
        torn.append(result)
        return SimpleNamespace(removed=["x"], kept_positions=[], failed=[], manual=[])

    model_key = "m"
    monkeypatch.setattr(bench.client, "run_case", fake_run_case)
    monkeypatch.setattr(bench.scorer, "score_case", fake_score_case)
    monkeypatch.setattr(bench.cleanup, "teardown", fake_teardown)
    monkeypatch.setattr(bench.cleanup, "created_resources", lambda result: ["an executor"])

    case = SimpleNamespace(id="c_safety_injection_001", type="consult", risk_level="read_only")
    asyncio.run(runner._run_trial(case, model_key, SimpleNamespace(load=lambda _: None), 1, [], {}))
    assert torn, "teardown skipped a read-only case whose trace created a resource"


def test_mock_gaps_turn_a_row_into_a_harness_artifact(monkeypatch):
    import bench.lanes
    import runner

    card = ScoreCard(case_id="c", model="m", answer_quality=0.0, answer_reason="", tool_accuracy=0.0,
                     latency_score=1.0, composite=0.0, latency_s=1.0, baseline_latency_s=1.0)
    monkeypatch.setattr(bench.lanes, "mock_state", lambda: {"unhandled": ["GET /controllers/"]})
    runner._flag_mock_gaps(card)
    assert "does not implement GET /controllers/" in card.harness_artifact


def _card(task_id: str, split: str, category: str, passed: bool) -> ScoreCard:
    return ScoreCard(
        case_id=task_id.lower(), model="m", answer_quality=0.9, answer_reason="",
        tool_accuracy=1.0, latency_score=1.0, composite=0.9 if passed else 0.2,
        latency_s=1.0, baseline_latency_s=1.0, task_id=task_id, split=split,
        task_category=category,
    )


def test_summary_breaks_down_by_category_and_split_and_names_regression_failures(tmp_path, monkeypatch):
    import bench.reporter as reporter

    monkeypatch.setattr(reporter, "RESULTS_DIR", tmp_path)
    cards = [
        _card("T-reads-001", "regression", "market_reads", True),
        _card("T-reads-002", "regression", "market_reads", False),
        _card("T-safety-001", "dev", "negative_safety", False),
    ]
    run_dir = reporter.save_run("m", cards, {}, "r1")
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["task_categories"]["market_reads"]["scored"] == 2
    assert summary["task_categories"]["market_reads"]["passed"] == 1
    assert summary["splits"]["dev"]["passed"] == 0
    assert summary["regression_failures"] == ["T-reads-002"], "dev failures are not regressions"
    record = json.loads((run_dir / "cases" / "t-reads-001.json").read_text())
    assert record["task_id"] == "T-reads-001" and record["split"] == "regression"


def test_task_set_version_tracks_task_versions_not_case_ids():
    from bench.manifest import task_set_version

    a = SimpleNamespace(id="c1", task={"task_id": "T-reads-001", "version": "1"})
    renamed = SimpleNamespace(id="renamed", task={"task_id": "T-reads-001", "version": "1"})
    bumped = SimpleNamespace(id="c1", task={"task_id": "T-reads-001", "version": "2"})
    assert task_set_version(cases=[a]) == task_set_version(cases=[renamed])
    assert task_set_version(cases=[a]) != task_set_version(cases=[bumped])


def test_tool_cases_now_carry_post_conditions():
    case = next(c for c in load_all_cases() if c.id == "tool_create_position_executor_002")
    assert case.post_conditions["list_executors"]["status"] == "RUNNING"


def test_an_unsafe_action_is_never_excluded_as_a_mock_gap(monkeypatch):
    import bench.lanes
    import runner

    card = ScoreCard(case_id="c", model="m", answer_quality=0.0, answer_reason="", tool_accuracy=0.0,
                     latency_score=1.0, composite=0.0, latency_s=1.0, baseline_latency_s=1.0,
                     forbidden_violations=["create_order_executor"])
    monkeypatch.setattr(bench.lanes, "mock_state", lambda: {"unhandled": ["POST /trading/positions"]})
    runner._flag_mock_gaps(card)
    assert card.harness_artifact is None, "the banned call must stay in the unsafe rate"
