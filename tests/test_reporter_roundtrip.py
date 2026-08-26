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


def test_harness_artifacts_stay_out_of_the_headline_averages(tmp_path, monkeypatch):
    """Six tick cases skipped by a warmup timeout entered composite_avg as 0.0.

    `_domain_breakdown` had always dropped harness artifacts, so the domain read
    "1 scored, 9 excluded" while the headline read a 0.76 average over 88 cases
    including six zeros the model never had a chance to earn. One definition of
    scored, or the summary contradicts its own breakdown.
    """
    import bench.reporter as reporter

    monkeypatch.setattr(reporter, "RESULTS_DIR", tmp_path)

    run_dir = save_run(
        "m",
        [
            _card("ran", 0.90),
            _card("skipped", 0.0, harness_artifact="market warmup failed — binance/BTC-USDT"),
        ],
        {},
        "smoke03",
    )
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["cases_total"] == 2
    assert summary["cases_scored"] == 1
    assert summary["composite_avg"] == 0.9
    assert summary["pass_rate"] == 1.0


def test_case_records_persist_the_dataset_layer(tmp_path, monkeypatch):
    """The dashboard's Type column reads ``case_type`` and cannot re-derive it.

    Chat-scoped Layer 3 cases were merged into the consult layer but kept their
    ``agent_*`` ids, so the dashboard's old id-prefix guess labelled eight of them
    "agent". The layer has to travel with the result.
    """
    import bench.reporter as reporter

    monkeypatch.setattr(reporter, "RESULTS_DIR", tmp_path)

    run_dir = save_run(
        "m",
        [_card("agent_condor_005", 0.9, case_type="consult")],
        {},
        "smoke04",
    )
    record = json.loads((run_dir / "cases" / "agent_condor_005.json").read_text())
    assert record["case_type"] == "consult"


def test_score_case_stamps_the_layer_off_the_case(monkeypatch):
    """score_case is the only place that knows a case's layer; it must record it."""
    import asyncio

    from bench.client import BenchmarkResult, TurnResult
    from bench.dataset import ConsultCase
    from bench.scorer import score_case

    case = ConsultCase(id="agent_condor_005", question="q", type="consult")
    result = BenchmarkResult(
        case_id=case.id,
        model="m",
        turns=[TurnResult(response="an answer", tool_calls=[], latency_s=1.0)],
        wiring={"api_url": "http://localhost:8000"},
    )

    async def fake_quality(self, *_a, **_k):
        return 1.0, "fine"

    monkeypatch.setattr(
        "metrics.answer_quality.AnswerQualityMetric.a_score", fake_quality
    )
    card = asyncio.run(score_case(case, result, 1.0))
    assert card.case_type == "consult"


def test_saved_run_is_readable_by_the_report_loader(tmp_path, monkeypatch):
    """A run that saves but cannot be loaded back is still a lost run."""
    import bench.reporter as reporter

    monkeypatch.setattr(reporter, "RESULTS_DIR", tmp_path)
    save_run("m", [_card("a", 0.9)], {"a": "x"}, "smoke03")

    runs = load_all_runs()
    assert len(runs) == 1
    assert runs[0]["cases_scored"] == 1


# ── one unjudged case must not destroy the run ────────────────────────────────
def test_a_case_the_judge_could_not_score_does_not_crash_the_summary():
    """This lost a completed 55-minute run: empty directory, no summary, no cases.

    `metrics.answer_quality.a_score` returns None with no `error` set on two paths —
    "No response produced." and "Judge error (not scored): …" — and that is correct:
    a judge that failed has said nothing about the model, so the tool evidence stands
    and the composite renormalises over what was measurable. But `_compute_summary`
    guarded tool_accuracy, tool_params and live_validity against None and not
    answer_quality, so such a row entered `_mean` and raised TypeError one line after
    `run_dir.mkdir()`. One malformed judge reply discarded all 80 case files.
    """
    from bench.reporter import _compute_summary

    scored = _card("ok", 0.9, answer_quality=0.9)
    unjudged = _card(
        "boom",
        0.55,
        answer_quality=None,
        answer_reason="Judge error (not scored): Expecting ',' delimiter",
    )
    assert unjudged.error is None, "the premise: a judge failure is not an error row"

    summary = _compute_summary("m", [scored, unjudged])

    assert summary["answer_quality_avg"] == 0.9, "averaged over what was judged"
    assert summary["cases_unjudged"] == 1
    assert summary["unjudged_cases"] == ["boom"]
    assert summary["cases_scored"] == 2, "the row still counts everywhere else"


def test_every_case_unjudged_leaves_the_average_empty_not_zero():
    """None says "not measured"; 0.0 would assert the model answered badly."""
    from bench.reporter import _compute_summary

    summary = _compute_summary("m", [_card("a", 0.55, answer_quality=None)])
    assert summary["answer_quality_avg"] is None
    assert summary["cases_unjudged"] == 1


def test_the_cases_survive_a_summary_that_cannot_be_computed(tmp_path, monkeypatch):
    """The evidence is the irreplaceable half; the headline can be recomputed.

    Re-running the suite costs the better part of an hour and cannot reproduce the
    same responses, so a run must never be traded for its summary.
    """
    import logging

    import bench.reporter as reporter

    monkeypatch.setattr(reporter, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(
        reporter,
        "_compute_summary",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("headline math exploded")),
    )
    logging.disable(logging.CRITICAL)
    try:
        run_dir = reporter.save_run("m", [_card("c1", 0.9), _card("c2", 0.9)], {"c1": "hello"}, "rid")
    finally:
        logging.disable(logging.NOTSET)

    assert sorted(p.stem for p in (run_dir / "cases").iterdir()) == ["c1", "c2"]
    summary = json.loads((run_dir / "summary.json").read_text())
    assert "headline math exploded" in summary["summary_error"]
    assert summary["cases_total"] == 2


def test_the_summary_survives_every_nullable_metric_being_none():
    """The class-level guard, not just the one field that cost a run.

    Four metrics are `float | None` — answer_quality, tool_accuracy, tool_params,
    live_validity — each None when there was no ground truth to score it against.
    latency_score and composite are plain floats and no code path sets them None.
    A row that measured nothing at all must still roll up.
    """
    from bench.reporter import _compute_summary

    nothing_measured = _card(
        "blank",
        0.0,
        answer_quality=None,
        tool_accuracy=None,
        tool_params=None,
        live_validity=None,
    )
    summary = _compute_summary("m", [nothing_measured])

    assert summary["answer_quality_avg"] is None
    assert summary["tool_accuracy_avg"] is None
    assert summary["tool_params_avg"] is None
    assert summary["live_validity_avg"] is None
    assert summary["cases_scored"] == 1
