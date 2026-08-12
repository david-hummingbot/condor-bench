"""Cancelling a run must keep what it measured, without reshaping the matrix.

A run cancelled at case 82 of 93 saved nothing: `save_run` sits after the case loop
and `CancelledError` unwound straight past it, so 81 scored cases — hours of real ACP
spend — were discarded and `results/` stayed empty.

Saving them is only half the fix. Matrix cells are claimed newest-run-wins, so a run
cancelled early would otherwise claim its model's domain and tool cells on a handful
of cases and shadow a complete run from the day before. A partial run is therefore
persisted but excluded from the matrix unless it explicitly opts in.

Also here: the epoch annotation. The judge reads tool arguments verbatim, and on c008
it called a valid `start_time=1786406400` "likely a future/incorrect epoch" and docked
a correct answer from 1.0 to 0.75. That number is 2026-08-11T00:00:00Z.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from bench.tool_digest import annotate_epochs
from config import summary_counts_for_matrix


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    path = tmp_path / "models.json"
    path.write_text(
        json.dumps(
            {"models": [{"key": "ollama:small:3b", "params_b": 3, "provider": "local"}]}
        )
    )
    return path


# ── Epoch annotation ──────────────────────────────────────────────────────────
def test_the_epoch_that_cost_c008_a_quarter_of_its_score_is_spelled_out():
    args = {"data_type": "orders", "start_time": 1786406400, "status": "FILLED"}
    out = annotate_epochs(args)
    assert out["start_time"] == "1786406400 (2026-08-11T00:00:00Z)"
    # Untouched fields stay exactly as they were, including their types.
    assert out["data_type"] == "orders"
    assert out["status"] == "FILLED"


def test_milliseconds_are_recognised_too():
    assert annotate_epochs({"ts": 1786406400000})["ts"].endswith("(2026-08-11T00:00:00Z)")


@pytest.mark.parametrize(
    "value",
    [
        200,          # limit
        5,            # leverage
        0,
        -1,
        1.5,          # a price
        999_999_999,  # just under the seconds band
        True,         # bool is an int subclass; never a timestamp
        False,
        "1786406400",  # already a string — leave the model's own formatting alone
    ],
)
def test_non_timestamps_are_left_alone(value):
    assert annotate_epochs({"k": value})["k"] == value


def test_nested_structures_are_annotated():
    out = annotate_epochs({"filter": {"since": 1786406400}, "list": [1786406400]})
    assert "2026-08-11" in out["filter"]["since"]
    assert "2026-08-11" in out["list"][0]


def test_the_judge_transcript_carries_the_annotation():
    """The whole point: it has to reach the text the judge actually reads."""
    from bench.client import BenchmarkResult, TurnResult

    turn = TurnResult(
        response="No trades in the last 24 hours.",
        tool_calls=[
            {
                "tool": "search_history",
                "tool_call_id": "c1",
                "args": {"data_type": "orders", "start_time": 1786406400},
            }
        ],
        latency_s=1.0,
        tool_responses=[
            {"tool": "search_history", "tool_call_id": "c1", "output": "No orders found."}
        ],
    )
    transcript = BenchmarkResult(case_id="c008", model="m", turns=[turn]).transcript_for_judge()
    assert "2026-08-11T00:00:00Z" in transcript, transcript


# ── Partial runs and the matrix ───────────────────────────────────────────────
def test_a_partial_run_does_not_feed_the_matrix():
    assert summary_counts_for_matrix({"run_type": "adhoc"}) is True
    assert summary_counts_for_matrix({"run_type": "adhoc", "partial": True}) is False
    # Explicit opt-in still wins, same as it does for suite runs.
    assert (
        summary_counts_for_matrix(
            {"run_type": "adhoc", "partial": True, "include_in_matrix": True}
        )
        is True
    )
    # `partial: False` is a complete run, not a partial one.
    assert summary_counts_for_matrix({"run_type": "adhoc", "partial": False}) is True


def test_a_partial_run_cannot_shadow_a_complete_one(tmp_path: Path, registry: Path):
    """The reason partial runs are excluded, stated as a test.

    Cells are claimed newest-first. Without the exclusion the one-case partial below
    would own this model's `general_consult` cell and the complete 3-case run from
    the day before would be locked out of it.
    """
    from bench.matrix import build_matrix

    results = tmp_path / "results"
    model = "ollama:small:3b"

    def write(name, ts, case_ids, extra):
        d = results / name
        (d / "cases").mkdir(parents=True)
        (d / "summary.json").write_text(
            json.dumps({"model": model, "timestamp": ts, "run_type": "adhoc", **extra})
        )
        for cid in case_ids:
            (d / "cases" / f"{cid}.json").write_text(
                json.dumps(
                    {
                        "case_id": cid,
                        "case_type": "consult",
                        "domain": "general_consult",
                        "risk_level": "read_only",
                        "composite": 0.9,
                    }
                )
            )

    write("aaa_full", "2026-08-11T00:00:00Z", ["c004", "c005", "c006"], {})
    write("bbb_partial", "2026-08-12T00:00:00Z", ["c004"], {"partial": True})

    cell = build_matrix(results_dir=results, models_path=registry)["domains"]["general_consult"][model]
    assert cell["scored"] == 3, (
        f"the partial run claimed the cell ({cell['scored']} case(s)) — a run cancelled "
        "early must not replace a complete one"
    )
    assert cell["run_dir"] == "aaa_full"


def test_cancelling_a_run_saves_the_cases_it_scored(monkeypatch: pytest.MonkeyPatch):
    """End to end through `_run_benchmark`: cancel mid-run, keep the scorecards."""
    import bench.baseline
    import bench.client
    import bench.dataset
    import bench.mcp_provider
    import bench.reporter
    import bench.scorer
    import bench.staging_health
    import bench.cleanup
    from dashboard.backend import app as A

    class FakeCase:
        def __init__(self, cid):
            self.id = cid
            self.type = "consult"
            self.domain = "general_consult"
            self.risk_level = "read_only"
            self.category = "everyday"
            self.agent_slug = None

    cases = [FakeCase(f"x{i:02d}") for i in range(10)]
    saved: dict = {}

    class FakeCard:
        def __init__(self, cid):
            self.case_id = cid
            self.composite = 0.9
            self.harness_artifact = None
            self.usage = {}

        def as_dict(self):
            return {"case_id": self.case_id, "composite": self.composite}

    class FakeResult:
        def __init__(self, cid):
            self.case_id = cid
            self.latency_s = 1.0
            self.response = "ok"

    async def fake_run_case(case, model):
        # Cancel the task from inside the loop, after 4 cases have been scored.
        if case.id == "x04":
            raise asyncio.CancelledError
        return FakeResult(case.id)

    def fake_save_run(model, scorecards, responses, run_id, *, prompts=None, extra_summary=None):
        saved["model"] = model
        saved["scorecards"] = list(scorecards)
        saved["summary"] = dict(extra_summary or {})
        return Path("results") / "partial_dir"

    monkeypatch.setattr(bench.staging_health, "a_assert_ready", lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(bench.dataset, "load_all_cases", lambda *a, **k: cases)
    monkeypatch.setattr(bench.dataset, "filter_cases", lambda cs, **k: list(cs))
    monkeypatch.setattr(bench.dataset, "case_prompt_map", lambda *a, **k: {})
    monkeypatch.setattr(bench.dataset, "is_mutating", lambda case: False)
    monkeypatch.setattr(bench.client, "run_case", fake_run_case)
    monkeypatch.setattr(bench.client, "case_input_text", lambda case: "q")
    monkeypatch.setattr(bench.scorer, "score_case", lambda c, r, b: _done(FakeCard(c.id)))
    monkeypatch.setattr(bench.reporter, "save_run", fake_save_run)
    monkeypatch.setattr(bench.mcp_provider, "target_banner", lambda: "test")
    monkeypatch.setattr(bench.baseline, "BaselineStore", lambda *a, **k: _NoBaselines())
    monkeypatch.setattr(bench.cleanup, "teardown", lambda *a, **k: _done(None))

    run_id = "testrun"
    A._active_runs[run_id] = {
        "run_id": run_id, "status": "starting", "events": [], "listeners": [],
        "task": None, "total": 0, "current_case": None, "next_case": None,
        "pause_event": asyncio.Event(),
    }
    req = A.RunRequest(models=[{"model_key": "ollama:small:3b"}])
    try:
        asyncio.run(A._run_benchmark(run_id, req))
        state = A._active_runs[run_id]
        assert state["status"] == "cancelled"
        assert saved.get("scorecards"), "cancelling discarded every scored case"
        assert [c.case_id for c in saved["scorecards"]] == ["x00", "x01", "x02", "x03"]
        summary = saved["summary"]
        assert summary["partial"] is True
        assert summary["cases_scored"] == 4
        assert summary["cases_planned"] == 10
        # The pin must describe what it measured, not what was planned.
        assert summary["case_ids"] == ["x00", "x01", "x02", "x03"]
        assert summary_counts_for_matrix(summary) is False
        assert state["partial_run_dirs"] == ["partial_dir"]
    finally:
        A._active_runs.clear()


def _done(value):
    fut: asyncio.Future = asyncio.Future()
    fut.set_result(value)
    return fut


class _NoBaselines:
    def load(self, case_id):
        return None

    def missing(self, case_ids):
        return []
