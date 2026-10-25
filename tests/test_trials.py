"""Phase 2: multiple trials per case, and what the matrix, router and compare do with them."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bench.compare import compare_summaries, paired_deltas
from bench.matrix import build_matrix
from bench.reporter import save_run, trial_key
from bench.routing import recommend
from bench.scorer import ScoreCard


def _card(case_id: str, *, trial_idx: int = 1, composite: float = 0.9, **kw) -> ScoreCard:
    base = dict(
        case_id=case_id,
        model="ollama:small:3b",
        answer_quality=0.9,
        answer_reason="",
        tool_accuracy=1.0,
        latency_score=1.0,
        composite=composite,
        latency_s=2.0,
        baseline_latency_s=2.0,
        domain="general_consult",
        trial_idx=trial_idx,
    )
    base.update(kw)
    return ScoreCard(**base)


# ── Running trials ─────────────────────────────────────────────────────────────
def test_three_trials_run_every_case_three_times(monkeypatch):
    import runner
    import bench.client
    import bench.market_warmup
    import bench.probe_journal
    import bench.scorer

    calls: list[str] = []

    async def fake_run_case(case, model):
        calls.append(case.id)
        return SimpleNamespace(latency_s=1.0, response=f"answer {len(calls)}")

    async def fake_score_case(case, result, baseline_latency, *, trial_idx=1):
        return _card(case.id, trial_idx=trial_idx)

    async def fake_warmup(case):
        return SimpleNamespace(ok=True, notes=[])

    monkeypatch.setattr(bench.client, "run_case", fake_run_case)
    monkeypatch.setattr(bench.scorer, "score_case", fake_score_case)
    monkeypatch.setattr(bench.market_warmup, "ensure_markets_for_case", fake_warmup)
    monkeypatch.setattr(bench.probe_journal, "ensure_probe_journal", lambda case: None)

    store = SimpleNamespace(load=lambda case_id: None)
    cases = [
        SimpleNamespace(id="c1", type="consult", risk_level="read_only"),
        SimpleNamespace(id="c2", type="consult", risk_level="read_only"),
    ]
    cards, responses = asyncio.run(runner._run_cases(cases, "m", store, trials=3))

    assert calls == ["c1", "c1", "c1", "c2", "c2", "c2"], "a case's trials run back to back"
    assert [(c.case_id, c.trial_idx) for c in cards] == [
        ("c1", 1), ("c1", 2), ("c1", 3), ("c2", 1), ("c2", 2), ("c2", 3),
    ]
    assert set(responses) == {"c1", "c1__t2", "c1__t3", "c2", "c2__t2", "c2__t3"}


def test_every_trial_gets_a_fresh_probe_journal(monkeypatch):
    """Set up once per case, trial 1's journal write satisfied trial 2's read-back."""
    import runner
    import bench.client
    import bench.market_warmup
    import bench.probe_journal
    import bench.scorer

    order: list[str] = []

    async def fake_run_case(case, model):
        order.append("run")
        return SimpleNamespace(latency_s=1.0, response="ok")

    async def fake_score_case(case, result, baseline_latency, *, trial_idx=1):
        return _card(case.id, trial_idx=trial_idx)

    async def fake_warmup(case):
        return SimpleNamespace(ok=True, notes=[])

    monkeypatch.setattr(bench.client, "run_case", fake_run_case)
    monkeypatch.setattr(bench.scorer, "score_case", fake_score_case)
    monkeypatch.setattr(bench.market_warmup, "ensure_markets_for_case", fake_warmup)
    monkeypatch.setattr(
        bench.probe_journal, "ensure_probe_journal", lambda case: order.append("fresh")
    )

    cases = [SimpleNamespace(id="c_journal_roundtrip_001", type="consult", risk_level="read_only")]
    asyncio.run(runner._run_cases(cases, "m", SimpleNamespace(load=lambda _: None), trials=2))
    # Once at case setup, then before each trial's run.
    assert order == ["fresh", "fresh", "run", "fresh", "run"]


def test_a_warmup_failure_costs_every_trial(monkeypatch):
    import runner
    import bench.market_warmup
    import bench.probe_journal

    async def failing_warmup(case):
        return SimpleNamespace(ok=False, notes=[])

    monkeypatch.setattr(bench.market_warmup, "ensure_markets_for_case", failing_warmup)
    monkeypatch.setattr(
        bench.market_warmup,
        "warmup_failure_card",
        lambda case, model, warmup: _card(case.id, harness_artifact="warmup failed"),
    )
    monkeypatch.setattr(bench.probe_journal, "ensure_probe_journal", lambda case: None)

    cases = [SimpleNamespace(id="c1", type="tick", risk_level="read_only")]
    cards, _ = asyncio.run(
        runner._run_cases(cases, "m", SimpleNamespace(load=lambda _: None), trials=3)
    )
    assert [c.trial_idx for c in cards] == [1, 2, 3]
    assert all(c.harness_artifact for c in cards)


def test_trial_files_do_not_overwrite_each_other(tmp_path, monkeypatch):
    import bench.reporter as reporter

    monkeypatch.setattr(reporter, "RESULTS_DIR", tmp_path)
    cards = [_card("c1", trial_idx=t, composite=0.9 if t != 2 else 0.1) for t in (1, 2, 3)]
    responses = {trial_key("c1", t): f"r{t}" for t in (1, 2, 3)}
    run_dir = save_run("m", cards, responses, "run1")

    files = sorted(p.name for p in (run_dir / "cases").glob("*.json"))
    assert files == ["c1.json", "c1__t2.json", "c1__t3.json"], (
        "trial 1 keeps the bare name, so single-trial runs look as they always did"
    )
    t2 = json.loads((run_dir / "cases" / "c1__t2.json").read_text())
    assert t2["trial_idx"] == 2 and t2["response"] == "r2"

    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["trials"] == 3
    assert summary["cases_unique"] == 1
    assert summary["pass_count"] == 2
    lo, hi = summary["pass_rate_ci"]
    assert lo < summary["pass_rate"] < hi
    assert summary["pass_consistency_std"] == pytest.approx(0.5774, abs=1e-4)
    assert summary["latency_s"]["median"] == 2.0


def test_cost_per_success_divides_all_spend_by_successes(tmp_path, monkeypatch):
    import bench.reporter as reporter

    monkeypatch.setattr(reporter, "RESULTS_DIR", tmp_path)
    cards = [
        _card("c1", composite=0.9, usage={"cost_usd": 0.01}),
        _card("c2", composite=0.1, usage={"cost_usd": 0.03}),
    ]
    run_dir = save_run("m", cards, {}, "run1")
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["cost_per_success"] == 0.04, "the failure's spend counts against the success"


# ── Matrix and router ──────────────────────────────────────────────────────────
DOMAIN = "general_consult"


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    path = tmp_path / "models.json"
    path.write_text(
        json.dumps(
            {
                "models": [
                    {"key": "ollama:small:3b", "params_b": 3, "provider": "local"},
                    {"key": "ollama:mid:14b", "params_b": 14, "provider": "local"},
                    {"key": "cloud:cheap", "params_b": None, "provider": "cloud"},
                    {"key": "cloud:dear", "params_b": None, "provider": "cloud"},
                    {"key": "cloud:unpriced", "params_b": None, "provider": "cloud"},
                ]
            }
        )
    )
    return path


def _write(results: Path, model: str, rows: list[dict]) -> None:
    run_dir = results / f"run_{model.replace(':', '_')}"
    (run_dir / "cases").mkdir(parents=True, exist_ok=True)
    (run_dir / "summary.json").write_text(
        json.dumps({"model": model, "timestamp": "2026-10-01T00:00:00Z"})
    )
    for row in rows:
        name = trial_key(row["case_id"], row.get("trial_idx", 1))
        (run_dir / "cases" / f"{name}.json").write_text(json.dumps(row))


def _rows(n_cases: int, trials: int, *, passed=True, cost=None, unsafe_on=(), **kw) -> list[dict]:
    out = []
    for i in range(n_cases):
        for t in range(1, trials + 1):
            cid = f"c{i}"
            out.append(
                {
                    "case_id": cid,
                    "trial_idx": t,
                    "domain": DOMAIN,
                    "composite": 0.9 if passed else 0.2,
                    "passed": passed,
                    "risk_level": "read_only",
                    "latency_s": 2.0,
                    "usage": {"cost_usd": cost} if cost is not None else {},
                    "forbidden_violations": ["manage_executors:create"] if cid in unsafe_on else [],
                    **kw,
                }
            )
    return out


def test_matrix_cells_carry_wilson_intervals_over_trials(tmp_path, registry):
    results = tmp_path / "results"
    _write(results, "ollama:small:3b", _rows(4, 3))
    cell = build_matrix(results_dir=results, models_path=registry)["domains"][DOMAIN][
        "ollama:small:3b"
    ]
    assert cell["scored"] == 12
    assert cell["trials"] == 3
    assert cell["pass_rate"] == 1.0
    assert cell["pass_rate_ci"][0] == pytest.approx(0.7575, abs=1e-3)
    assert cell["gate_status"] == "not_yet_passing"
    assert cell["consistency_std"] == 0.0


def test_matrix_uses_the_recorded_verdict_not_the_composite(tmp_path, registry):
    """A banned call fails a case even when the composite would clear 0.70."""
    results = tmp_path / "results"
    rows = _rows(2, 1)
    rows[0]["passed"] = False  # hard-gated, composite still 0.9
    _write(results, "ollama:mid:14b", rows)
    cell = build_matrix(results_dir=results, models_path=registry)["domains"][DOMAIN][
        "ollama:mid:14b"
    ]
    assert cell["passed"] == 1


def test_an_interval_that_straddles_the_gate_is_not_yet_passing(tmp_path, registry):
    results = tmp_path / "results"
    _write(results, "ollama:small:3b", _rows(4, 1))  # 4/4: lower bound ~0.51
    routing = recommend(
        build_matrix(results_dir=results, models_path=registry), models_path=registry
    )
    assert DOMAIN not in routing["recommendations"]
    gap = routing["unmet_domains"][DOMAIN]
    assert gap["status"] == "not_yet_passing"
    assert "straddles" in " ".join(gap["blockers"]["ollama:small:3b"])


def test_an_interval_wholly_below_the_gate_is_failing(tmp_path, registry):
    results = tmp_path / "results"
    _write(results, "ollama:small:3b", _rows(10, 2, passed=False))
    routing = recommend(
        build_matrix(results_dir=results, models_path=registry), models_path=registry
    )
    assert routing["unmet_domains"][DOMAIN]["status"] == "failing"


def test_a_single_unsafe_action_blocks_the_route(tmp_path, registry):
    results = tmp_path / "results"
    _write(results, "ollama:small:3b", _rows(8, 3, unsafe_on={"c0"}))
    _write(results, "ollama:mid:14b", _rows(8, 3))
    routing = recommend(
        build_matrix(results_dir=results, models_path=registry), models_path=registry
    )
    rec = routing["recommendations"][DOMAIN]
    assert rec["model"] == "ollama:mid:14b", "the smaller model made a banned call"
    assert rec["unsafe"] == 0


def test_cheapest_per_success_wins_and_unpriced_cloud_sorts_last(tmp_path, registry):
    results = tmp_path / "results"
    _write(results, "cloud:dear", _rows(8, 3, cost=0.05))
    _write(results, "cloud:cheap", _rows(8, 3, cost=0.01))
    _write(results, "cloud:unpriced", _rows(8, 3))
    routing = recommend(
        build_matrix(results_dir=results, models_path=registry), models_path=registry
    )
    rec = routing["recommendations"][DOMAIN]
    assert rec["model"] == "cloud:cheap"
    assert rec["cost_per_success"] == pytest.approx(0.01)
    assert [a["model"] for a in rec["alternatives"]] == ["cloud:dear", "cloud:unpriced"]


def test_local_models_count_as_free_and_params_break_the_tie(tmp_path, registry):
    results = tmp_path / "results"
    _write(results, "ollama:mid:14b", _rows(8, 3))
    _write(results, "ollama:small:3b", _rows(8, 3))
    _write(results, "cloud:cheap", _rows(8, 3, cost=0.001))
    routing = recommend(
        build_matrix(results_dir=results, models_path=registry), models_path=registry
    )
    assert routing["recommendations"][DOMAIN]["model"] == "ollama:small:3b"


def test_the_cost_frontier_flags_undominated_models(tmp_path, registry):
    results = tmp_path / "results"
    _write(results, "ollama:small:3b", _rows(8, 3, passed=False))  # free, 0%
    _write(results, "cloud:cheap", _rows(8, 3, cost=0.01))  # 100%, cheap
    _write(results, "cloud:dear", _rows(8, 3, cost=0.05))  # 100%, dear — dominated
    _write(results, "cloud:unpriced", _rows(8, 3))  # no cost axis
    routing = recommend(
        build_matrix(results_dir=results, models_path=registry), models_path=registry
    )
    frontier = {r["model"]: r for r in routing["frontiers"][DOMAIN]}
    assert frontier["cloud:cheap"]["on_frontier"] is True
    assert frontier["ollama:small:3b"]["on_frontier"] is True, "nothing is cheaper than free"
    assert frontier["cloud:dear"]["on_frontier"] is False
    assert frontier["cloud:unpriced"]["on_frontier"] is False
    assert frontier["cloud:unpriced"]["unpriced"] is True
    assert frontier["cloud:cheap"]["pass_rate_ci"][0] > 0.8


# ── Compare ────────────────────────────────────────────────────────────────────
def _member(run_dir: str, model: str, trials: int) -> dict:
    return {
        "run_dir": run_dir,
        "model": model,
        "models": [model],
        "case_ids": ["c0", "c1"],
        "condor": {"commit": "abc", "dirty_files": 0},
        "manifest": {"trials": trials, "experiment_type": "B", "env_mode": "live"},
    }


def test_compare_refuses_different_trial_counts_even_as_the_only_difference():
    comp = compare_summaries([_member("a", "m", 1), _member("b", "m", 3)])
    assert comp["comparable"] is False
    assert comp["differences"] == ["manifest:trials"]


def test_compare_pairs_rows_by_case_and_trial():
    base = _rows(2, 3, passed=False)
    other = _rows(2, 3)
    other.append({"case_id": "extra", "trial_idx": 1, "composite": 0.9, "passed": True})
    comp = compare_summaries(
        [_member("a", "m1", 3), _member("b", "m2", 3)], {"a": base, "b": other}
    )
    assert comp["comparable"] is True
    paired = comp["deltas"]["pairs"][0]["paired"]
    assert paired["passed"]["n"] == 6
    assert paired["passed"]["mean_delta"] == 1.0
    assert paired["composite"]["mean_delta"] == pytest.approx(0.7)
    assert paired["unpaired"] == 1


def test_paired_deltas_skip_harness_artifacts():
    base = [{"case_id": "c", "trial_idx": 1, "composite": 0.9, "passed": True}]
    other = [{"case_id": "c", "trial_idx": 1, "composite": 0.0, "harness_artifact": "x"}]
    out = paired_deltas(base, other)
    assert out["composite"]["n"] == 0
    assert out["unpaired"] == 1
