"""Phase 6: experiment presets, contract promotion, release claims, gate targets."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bench.dataset import load_holdout_cases


# ── Presets ────────────────────────────────────────────────────────────────────
def _write_preset(tmp_path: Path, body: str) -> str:
    path = tmp_path / "p.yaml"
    path.write_text(body)
    return str(path)


def test_shipped_presets_are_valid():
    from bench.presets import list_presets, load_preset

    assert {"a_example", "b_example"} <= set(list_presets())
    assert load_preset("a_example")["experiment_type"] == "A"
    assert load_preset("b_example")["experiment_type"] == "B"


@pytest.mark.parametrize("body,match", [
    ("experiment_type: C\n", "must be A or B"),
    ("experiment_type: A\nfreeze: {model_id: m}\nvary: [model_id]\n", "cannot vary"),
    ("experiment_type: A\nfreeze: {}\n", "must pin freeze.model_id"),
    ("experiment_type: B\nfreeze: {require_clean: false}\n", "require_clean"),
    ("experiment_type: A\nfreeze: {model_id: m, inference_config: {temp: 1}}\n", "unknown inference_config"),
])
def test_malformed_presets_are_refused_at_load(tmp_path, body, match):
    from bench.presets import PresetError, load_preset

    with pytest.raises(PresetError, match=match):
        load_preset(_write_preset(tmp_path, body))


def test_a_preset_cannot_be_run_adhoc_or_as_the_other_experiment():
    from bench.presets import PresetError, apply_preset

    with pytest.raises(PresetError, match="adhoc"):
        apply_preset({}, "a_example", adhoc=True)
    with pytest.raises(PresetError, match="contradicts"):
        apply_preset({}, "a_example", experiment_type="B")


def test_an_experiment_a_preset_refuses_a_second_model(monkeypatch):
    from bench.presets import apply_preset
    from bench.suite_runner import FreezeViolationError, check_experiment_freeze

    monkeypatch.delenv("BENCH_TEMPERATURE", raising=False)
    suite = apply_preset({"id": "s"}, "a_example")
    assert suite["experiment_type"] == "A" and suite["preset"] == "a_example"
    pinned = suite["freeze"]["model_id"]
    assert check_experiment_freeze(suite, ["e1", "e2"], [{"key": pinned}]) == "A"
    with pytest.raises(FreezeViolationError):
        check_experiment_freeze(suite, ["e1"], [{"key": pinned}, {"key": "openai:gpt-4o"}])
    with pytest.raises(FreezeViolationError, match="preset pins model"):
        check_experiment_freeze(suite, ["e1"], [{"key": "openai:gpt-4o"}])


def test_a_suite_cannot_save_a_preset_that_does_not_load(tmp_path, monkeypatch):
    import bench.suites as suites

    monkeypatch.setattr(suites, "SUITES_DIR", tmp_path / "suites")
    monkeypatch.setattr(suites, "ENVIRONMENTS_DIR", tmp_path / "envs")
    with pytest.raises(suites.SuiteStoreError, match="No preset"):
        suites.create_suite({"id": "s1", "preset": "nope"})
    record = suites.create_suite({"id": "s2", "preset": "b_example"})
    assert record["preset"] == "b_example"


# ── Contract promotion ─────────────────────────────────────────────────────────
HOLDOUT = sorted(c.task["task_id"] for c in load_holdout_cases())


def _row(task_id: str, *, first: float, tokens: int, ttfva: float, repairs: int = 0,
         passed: bool = True, unsafe: bool = False, category: str = "market_reads") -> dict:
    responses = [{"score": 0.0}] * repairs + [{"score": 1.0}]
    return {
        "case_id": task_id.lower(), "task_id": task_id, "split": "holdout",
        "task_category": category, "passed": passed, "composite": 0.9 if passed else 0.1,
        "first_call_validity": first, "time_to_first_valid_action": ttfva,
        "latency_representative": True, "usage": {"total_tokens": tokens},
        "live_validity_detail": {"responses": responses},
        "forbidden_violations": ["create_order_executor"] if unsafe else [],
        "harness_artifact": None, "error": None,
    }


def _run(results: Path, name: str, commit: str, rows: list[dict], *, lane: str = "live",
         exp: str = "A") -> str:
    run = results / name
    (run / "cases").mkdir(parents=True)
    (run / "summary.json").write_text(json.dumps({
        "model": "m", "models": ["m"], "case_ids": ["x"],
        "condor": {"commit": commit, "dirty_files": 0},
        "manifest": {"env_mode": lane, "experiment_type": exp, "trials": 1,
                     "contract_version": f"contract-{commit}"},
    }))
    for i, r in enumerate(rows):
        (run / "cases" / f"{i}.json").write_text(json.dumps(r))
    return name


def _ladder(results: Path, ladder_id: str, commit: str, *, l0_ok=True, l1_rate=0.9, unsafe=0.0):
    (results / f"ladder_{ladder_id}").mkdir(parents=True)
    (results / f"ladder_{ladder_id}" / "l0.json").write_text(json.dumps(
        {"failed": [] if l0_ok else [{"case": "schema:x"}], "condor": {"commit": commit}}))
    (results / f"{ladder_id}l1_m").mkdir(parents=True)
    (results / f"{ladder_id}l1_m" / "summary.json").write_text(json.dumps(
        {"pass_rate": l1_rate, "unsafe_action_rate": unsafe}))


@pytest.fixture
def results(tmp_path, monkeypatch):
    import bench.promotion as promotion

    root = tmp_path / "results"
    root.mkdir()
    monkeypatch.setattr(promotion, "RESULTS_DIR", root)
    return root


def _good_pair(results: Path) -> tuple[str, str]:
    base = [_row(t, first=0.6, tokens=10_000, ttfva=10.0, repairs=1) for t in HOLDOUT]
    cand = [_row(t, first=0.8, tokens=8_000, ttfva=8.0, repairs=0) for t in HOLDOUT]
    return _run(results, "base", "aaa111", base), _run(results, "cand", "bbb222", cand)


def test_a_candidate_that_clears_every_gate_and_has_its_bits_claims_promotion(results):
    from bench.promotion import build_report

    base, cand = _good_pair(results)
    _ladder(results, "lad1", "bbb222")
    report = build_report(base, cand, ladder_id="lad1")
    assert report["gates_passed"], report["gates"]
    assert report["lower_layer_bits"] == {"l0_passed": True, "l1_passed": True}
    assert report["claims_live_promotion"], report["claim_blockers"]


def test_no_live_claim_without_lower_layer_bits(results):
    from bench.promotion import build_report

    base, cand = _good_pair(results)
    report = build_report(base, cand)
    assert report["gates_passed"]
    assert not report["claims_live_promotion"]
    assert any("lower-layer" in b for b in report["claim_blockers"])


def test_bits_from_another_commit_do_not_count(results):
    from bench.promotion import build_report

    base, cand = _good_pair(results)
    _ladder(results, "lad2", "zzz999")
    report = build_report(base, cand, ladder_id="lad2")
    assert not report["claims_live_promotion"]
    assert any("L0 ran on condor zzz999" in b for b in report["claim_blockers"])


def test_no_live_claim_on_the_mock_lane_or_with_partial_holdout(results):
    from bench.promotion import build_report

    base = [_row(t, first=0.6, tokens=10_000, ttfva=10.0, repairs=1) for t in HOLDOUT]
    cand = [_row(t, first=0.8, tokens=8_000, ttfva=8.0) for t in HOLDOUT[:-1]]
    b = _run(results, "base", "aaa111", base, lane="mock")
    c = _run(results, "cand", "bbb222", cand, lane="mock")
    _ladder(results, "lad3", "bbb222")
    blockers = build_report(b, c, ladder_id="lad3")["claim_blockers"]
    assert any("not a live-lane run" in x for x in blockers)
    assert any("holdout not fully covered" in x for x in blockers)


def test_a_missed_gate_blocks_the_claim(results):
    from bench.promotion import build_report

    base = [_row(t, first=0.6, tokens=10_000, ttfva=10.0) for t in HOLDOUT]
    cand = [_row(t, first=0.8, tokens=9_500, ttfva=8.0) for t in HOLDOUT]  # only −5% tokens
    _ladder(results, "lad4", "bbb222")
    report = build_report(_run(results, "base", "aaa111", base), _run(results, "cand", "bbb222", cand),
                          ladder_id="lad4")
    gate = next(g for g in report["gates"] if g["gate"] == "tokens_per_success")
    assert gate["passed"] is False and gate["delta"] == pytest.approx(-0.05)
    assert "gate failed: tokens_per_success" in report["claim_blockers"]


def test_a_safety_regression_on_holdout_blocks_the_claim(results):
    from bench.promotion import build_report

    base = [_row(t, first=0.6, tokens=10_000, ttfva=10.0, repairs=1) for t in HOLDOUT]
    cand = [_row(t, first=0.8, tokens=8_000, ttfva=8.0, unsafe=(t == HOLDOUT[0])) for t in HOLDOUT]
    report = build_report(_run(results, "base", "aaa111", base), _run(results, "cand", "bbb222", cand))
    gate = report["gates"][-1]
    assert gate["gate"] == "safety_regressions_on_holdout"
    assert gate["tasks"] == [HOLDOUT[0]] and not gate["passed"]


def test_promotion_refuses_runs_that_are_not_an_experiment_a_pair(results):
    from bench.promotion import PromotionError, build_report

    rows = [_row(t, first=0.6, tokens=10_000, ttfva=10.0) for t in HOLDOUT]
    same = _run(results, "same1", "aaa111", rows), _run(results, "same2", "aaa111", rows)
    with pytest.raises(PromotionError, match="two Condor checkouts"):
        build_report(*same)
    b_runs = _run(results, "b1", "aaa111", rows, exp="B"), _run(results, "b2", "bbb222", rows, exp="B")
    with pytest.raises(PromotionError, match="experiment A"):
        build_report(*b_runs)
    no_holdout = [{**r, "split": "regression"} for r in rows]
    nh = _run(results, "nh1", "aaa111", no_holdout), _run(results, "nh2", "bbb222", no_holdout)
    with pytest.raises(PromotionError, match="holdout"):
        build_report(*nh)


# ── Gate targets ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("target", ["test-pr", "test-nightly", "test-release", "promote"])
def test_gate_targets_exist(target):
    from config import ROOT

    proc = subprocess.run(["make", "-n", target], cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_the_decision_log_template_names_every_gate():
    from config import ROOT

    text = (ROOT / "docs" / "decision_log_template.md").read_text()
    for needle in ("Frontier", "+15 pp", "−20%", "−15%", "safety regressions", "Fallback"):
        assert needle in text


def test_bits_from_a_dirty_checkout_do_not_count(results):
    from bench.promotion import build_report

    base, cand = _good_pair(results)
    _ladder(results, "lad5", "bbb222")
    l0 = results / "ladder_lad5" / "l0.json"
    data = json.loads(l0.read_text())
    data["condor"]["dirty_files"] = 2
    l0.write_text(json.dumps(data))
    report = build_report(base, cand, ladder_id="lad5")
    assert not report["claims_live_promotion"]
    assert any("dirty condor checkout" in b for b in report["claim_blockers"])
