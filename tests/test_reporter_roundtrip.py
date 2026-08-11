"""save_run must work against real ScoreCard objects.

This file exists because of a specific escape. The live-only refactor removed
``ScoreCard.mode``, but ``reporter._compute_summary`` still did
``{sc.mode for sc in scorecards}``. Nothing caught it: the sweeps grepped for
``bench_mode`` / ``BENCH_MODE`` / ``score_weights``, none of which match ``sc.mode``,
and no test ever called save_run with real scorecards — the matrix tests all write
summary.json by hand.

So every run scored all its cases, ran teardown, then died with an AttributeError at
save time and lost the whole run. A ~50 minute sweep would have produced nothing.

The lesson these tests encode: the persistence path has to be exercised with the real
objects, not with hand-built dicts that cannot go stale.
"""

from __future__ import annotations

import json

from bench.reporter import load_all_runs, save_run
from bench.scorer import ScoreCard


def _card(case_id: str, composite: float, **kw) -> ScoreCard:
    base = dict(
        case_id=case_id,
        model="anthropic:claude-sonnet-5",
        answer_quality=0.9,
        answer_reason="fine",
        tool_accuracy=1.0,
        latency_score=1.0,
        composite=composite,
        latency_s=12.0,
        baseline_latency_s=10.0,
        domain="market_making_expert",
        risk_level="read_only",
    )
    base.update(kw)
    return ScoreCard(**base)


def test_save_run_persists_real_scorecards(tmp_path, monkeypatch):
    """The exact call that crashed the first live smoke run."""
    import bench.reporter as reporter

    monkeypatch.setattr(reporter, "RESULTS_DIR", tmp_path)

    run_dir = save_run(
        "anthropic:claude-sonnet-5",
        [_card("a", 0.87), _card("b", 0.42)],
        {"a": "answer a", "b": "answer b"},
        "smoke01",
        prompts={"a": "question a", "b": "question b"},
        extra_summary={"run_type": "adhoc"},
    )

    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["cases_total"] == 2
    assert summary["cases_scored"] == 2
    assert summary["model"] == "anthropic:claude-sonnet-5"
    assert summary["run_type"] == "adhoc"
    # The removed field must not have come back in any guise.
    assert "mode" not in summary

    cases = sorted((run_dir / "cases").glob("*.json"))
    assert len(cases) == 2


def test_summary_counts_capped_post_condition_failures_separately(tmp_path, monkeypatch):
    """A capped case is a model failure, not a harness artifact — they must not merge."""
    import bench.reporter as reporter

    monkeypatch.setattr(reporter, "RESULTS_DIR", tmp_path)

    run_dir = save_run(
        "m",
        [
            _card("built", 0.95),
            _card("not-built", 0.50, post_condition_failed="post-condition not met for x"),
            _card("misscoped", 0.10, harness_artifact="assistant prompt fell back"),
        ],
        {},
        "smoke02",
    )
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["post_condition_failures"] == 1
    assert summary["post_condition_failure_cases"][0]["case_id"] == "not-built"
    assert summary["harness_artifacts"] == 1


def test_saved_run_is_readable_by_the_report_loader(tmp_path, monkeypatch):
    """A run that saves but cannot be loaded back is still a lost run."""
    import bench.reporter as reporter

    monkeypatch.setattr(reporter, "RESULTS_DIR", tmp_path)
    save_run("m", [_card("a", 0.9)], {"a": "x"}, "smoke03")

    runs = load_all_runs()
    assert len(runs) == 1
    assert runs[0]["cases_scored"] == 1
