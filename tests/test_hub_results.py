"""The Agents page groups hub runs by agent, then model — and scores only what was scored."""

from __future__ import annotations

import json
from pathlib import Path

from bench.hub_results import hub_agent_board


def _run(root: Path, name: str, *, slug="churn", model="m:a", digest="d1", ts="2026-11-10T00:00:00Z",
         records=(), run_type="hub_agent"):
    d = root / name
    (d / "cases").mkdir(parents=True)
    (d / "summary.json").write_text(json.dumps({
        "model": model, "timestamp": ts, "run_type": run_type, "trials": 1,
        "hub_agent": {"slug": slug, "name": slug.title(), "version": 1, "digest": digest,
                      "skills": ["s"], "loops": ["l"], "findings": []},
    }))
    for i, rec in enumerate(records):
        (d / "cases" / f"c{i}.json").write_text(json.dumps({"model": model, **rec}))


def _rec(case, composite, passed=True, **kw):
    return {"case_id": f"hub_churn__{case}", "composite": composite, "passed": passed,
            "answer_quality": composite, "error": None, "harness_artifact": None,
            "tool_calls": [{}] * 3, "model_time_s": 10.0, **kw}


def test_models_are_ranked_per_agent_over_their_scored_cases(tmp_path):
    _run(tmp_path, "r1", model="m:a", records=[_rec("state_check", 0.9), _rec("skill_x", 0.7, passed=False)])
    _run(tmp_path, "r2", model="m:a", ts="2026-11-11T00:00:00Z", records=[_rec("state_check", 0.8)])
    _run(tmp_path, "r3", model="m:b", records=[_rec("state_check", 0.95)])

    (agent,) = hub_agent_board(tmp_path)["agents"]
    assert [m["model"] for m in agent["models"]] == ["m:b", "m:a"]
    a = agent["models"][1]
    assert a["runs"] == 2 and a["cases_scored"] == 3
    assert a["composite"] == 0.8 and a["passed"] == 2
    state = next(c for c in agent["cases"] if c["label"] == "state check")
    assert state["by_model"]["m:a"]["composite"] == 0.85
    assert state["by_model"]["m:a"]["trials"] == 2


def test_a_run_with_nothing_scored_reads_not_scored_not_zero(tmp_path):
    _run(tmp_path, "good", model="m:a", records=[_rec("state_check", 0.9)])
    _run(tmp_path, "hung", model="m:b", records=[_rec("state_check", 0.0, passed=False, harness_artifact="timeout")])

    (agent,) = hub_agent_board(tmp_path)["agents"]
    hung = next(m for m in agent["models"] if m["model"] == "m:b")
    assert hung["composite"] is None and hung["runs_not_scored"] == 1
    assert agent["models"][-1]["model"] == "m:b"
    run = next(r for r in agent["runs"] if r["run_dir"] == "hung")
    assert run["cases_scored"] == 0 and run["not_scored_reasons"] == ["timeout"]


def test_only_the_current_agent_version_is_scored(tmp_path):
    _run(tmp_path, "old", model="m:a", digest="old", records=[_rec("state_check", 0.1)])
    _run(tmp_path, "new", model="m:a", digest="new", ts="2026-11-11T00:00:00Z", records=[_rec("state_check", 0.9)])

    (agent,) = hub_agent_board(tmp_path)["agents"]
    assert agent["models"][0]["composite"] == 0.9
    assert {r["run_dir"]: r["current_version"] for r in agent["runs"]} == {"new": True, "old": False}


def test_library_runs_and_other_agents_stay_apart(tmp_path):
    _run(tmp_path, "lib", run_type="library", records=[_rec("state_check", 0.5)])
    _run(tmp_path, "c", slug="churn", records=[_rec("state_check", 0.9)])
    _run(tmp_path, "r", slug="regime", records=[_rec("state_check", 0.9)])

    assert sorted(a["slug"] for a in hub_agent_board(tmp_path)["agents"]) == ["churn", "regime"]


def test_the_dashboard_serves_the_board(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import dashboard.backend.app as app_mod

    _run(tmp_path, "c", records=[_rec("state_check", 0.9)])
    monkeypatch.setattr(app_mod, "RESULTS_DIR", tmp_path)
    body = TestClient(app_mod.app).get("/api/hub-agents").json()
    assert body["agents"][0]["slug"] == "churn"


def test_an_unsafe_call_counts_even_when_the_row_was_not_scored(tmp_path):
    """A harness fault spoils the score; it cannot un-place an order."""
    _run(tmp_path, "r", model="m:a", records=[
        _rec("state_check", 0.9),
        _rec("loop_l", 0.0, passed=False, harness_artifact="timeout",
             forbidden_violations=["create_order_executor"]),
        _rec("skill_s", 0.8, unsafe_action_rate=0.5),
    ])
    (agent,) = hub_agent_board(tmp_path)["agents"]
    assert agent["models"][0]["unsafe"] == 2


def test_per_success_figures_charge_the_failures_like_the_leaderboard(tmp_path):
    _run(tmp_path, "r", model="m:a", records=[
        _rec("state_check", 0.9, model_time_s=10.0, usage={"cost_usd": 0.02}),
        _rec("skill_s", 0.2, passed=False, model_time_s=30.0, usage={"cost_usd": 0.06}),
        # Passed but unpriced: it buys no share of the priced spend.
        _rec("loop_l", 0.9, model_time_s=20.0),
        # A clock that does not measure the model is left out of model time.
        _rec("skill_t", 0.9, model_time_s=999.0, latency_representative=False),
    ])
    (agent,) = hub_agent_board(tmp_path)["agents"]
    m = agent["models"][0]
    assert m["model_time_per_success"] == 30.0, "(10 + 30 + 20) / 2 timed successes"
    assert m["cost_per_success"] == 0.08, "(0.02 + 0.06) / 1 priced success"
