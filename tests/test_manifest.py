"""Tests for Phase 0: run manifest identity and experiment freeze enforcement."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bench.compare import compare_summaries, diff_manifests
from bench.client import InferenceConfig, _make_client, inference_config
from bench.manifest import (
    build_manifest,
    contract_version,
    grader_version,
    task_set_version,
)
from bench.suite_runner import FreezeViolationError, check_experiment_freeze


def test_build_manifest_contains_all_identity_fields():
    manifest_pin = build_manifest(
        run_type="adhoc",
        case_ids=["case_001", "case_002"],
        models=["anthropic:claude-sonnet-5"],
    )

    # Backwards-compatible keys
    assert manifest_pin["run_type"] == "adhoc"
    assert manifest_pin["case_ids"] == ["case_001", "case_002"]
    assert manifest_pin["models"] == ["anthropic:claude-sonnet-5"]
    assert "condor" in manifest_pin

    # New manifest block
    manifest = manifest_pin.get("manifest")
    assert isinstance(manifest, dict)

    required_keys = {
        "contract_version",
        "task_set_version",
        "grader_version",
        "inference_config",
        "env_mode",
        "experiment_type",
        "uv_lock_hash",
        "runtime",
    }
    assert required_keys.issubset(manifest.keys())

    # Inference config keys
    inf = manifest["inference_config"]
    assert isinstance(inf, dict)
    assert set(inf.keys()) == {"temperature", "top_p", "max_tokens", "reasoning", "seed"}

    # Runtime settings
    runtime = manifest["runtime"]
    assert "timeout_baseline_multiple" in runtime
    assert "timeout_min_s" in runtime
    assert "timeout_max_s" in runtime
    assert "concurrency" in runtime
    assert "cache" in runtime

    # Task set version changes when cases change
    v1 = task_set_version(["case_001"])
    v2 = task_set_version(["case_002"])
    assert v1 != v2
    assert v1 == task_set_version(["case_001"])


def test_inference_config_reads_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BENCH_TEMPERATURE", "0.7")
    monkeypatch.setenv("BENCH_TOP_P", "0.95")
    monkeypatch.setenv("BENCH_MAX_TOKENS", "4096")
    monkeypatch.setenv("BENCH_REASONING", "true")
    monkeypatch.setenv("BENCH_SEED", "42")

    cfg = inference_config()
    assert cfg.temperature == 0.7
    assert cfg.top_p == 0.95
    assert cfg.max_tokens == 4096
    assert cfg.reasoning is True
    assert cfg.seed == 42


def test_diff_manifests_detects_one_or_more_variables():
    base_manifest = {
        "contract_version": "hash_a",
        "task_set_version": "tasks_a",
        "grader_version": "grader_a",
        "inference_config": {"temperature": 0.0},
        "env_mode": "live",
        "experiment_type": "A",
        "uv_lock_hash": "uv_a",
        "runtime": {"timeout_min_s": 5},
    }

    # Same manifest -> no diff
    m1 = {"manifest": dict(base_manifest)}
    m2 = {"manifest": dict(base_manifest)}
    assert diff_manifests([m1, m2]) == []

    # One variable diff: contract_version
    m2["manifest"]["contract_version"] = "hash_b"
    assert diff_manifests([m1, m2]) == ["contract_version"]

    # Multiple diffs: contract_version and inference_config
    m2["manifest"]["inference_config"] = {"temperature": 0.5}
    diffs = diff_manifests([m1, m2])
    assert set(diffs) == {"contract_version", "inference_config"}


def test_compare_summaries_with_manifest_diff():
    s1 = {
        "run_dir": "run_1",
        "status": "completed",
        "model": "model_a",
        "cases_scored": 10,
        "cases_total": 10,
        "case_ids": ["c1", "c2"],
        "manifest": {
            "contract_version": "hash_a",
            "task_set_version": "tasks_a",
            "grader_version": "grader_a",
            "inference_config": {},
            "env_mode": "live",
            "experiment_type": "A",
            "uv_lock_hash": "uv_a",
            "runtime": {},
        },
    }
    s2 = dict(s1)
    s2["run_dir"] = "run_2"
    s2["manifest"] = dict(s1["manifest"])
    s2["manifest"]["contract_version"] = "hash_b"

    # One variable (the contract, i.e. the condor checkout) differs: comparable.
    comp = compare_summaries([s1, s2])
    assert comp["comparable"] is True
    assert comp["differences"] == []
    assert comp["variable_under_test"] == "condor_checkout"
    assert comp["manifest_diffs"] == ["contract_version"]

    # A second variable makes it a confounded comparison: refused, both listed.
    s2["manifest"]["grader_version"] = "grader_b"
    comp = compare_summaries([s1, s2])
    assert comp["comparable"] is False
    assert comp["variable_under_test"] is None
    assert set(comp["differences"]) == {"condor_checkout", "manifest:grader_version"}


def test_experiment_a_freeze_enforcement():
    suite = {"id": "suite_a", "experiment_type": "A"}
    env_ids = ["env_clean_1", "env_clean_2"]

    # Single model -> passes
    model_cfgs_ok = [{"key": "anthropic:claude-sonnet-5"}]
    res = check_experiment_freeze(suite, env_ids, model_cfgs_ok)
    assert res == "A"

    # Multiple models -> fails on Experiment A
    model_cfgs_bad = [
        {"key": "anthropic:claude-sonnet-5"},
        {"key": "openai:gpt-4o"},
    ]
    with pytest.raises(FreezeViolationError, match="expected a single frozen model"):
        check_experiment_freeze(suite, env_ids, model_cfgs_bad)

    # With adhoc=True -> bypasses freeze
    res_adhoc = check_experiment_freeze(suite, env_ids, model_cfgs_bad, adhoc=True)
    assert res_adhoc == "adhoc"


def test_experiment_b_freeze_enforcement():
    suite = {"id": "suite_b", "experiment_type": "B"}
    model_cfgs = [
        {"key": "anthropic:claude-sonnet-5"},
        {"key": "openai:gpt-4o"},
    ]

    # Multiple environments -> fails on Experiment B (Condor must be frozen)
    with pytest.raises(FreezeViolationError, match="expected a single frozen Condor environment"):
        check_experiment_freeze(suite, ["env_1", "env_2"], model_cfgs)

    # Single environment, dirty checkout -> fails
    with patch("bench.suite_runner.get_environment", return_value={"condor_path": "/fake/condor"}), \
         patch("bench.suite_runner.Path.is_dir", return_value=True), \
         patch("subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="M modified_file.py\n")
        with pytest.raises(FreezeViolationError, match="uncommitted changes"):
            check_experiment_freeze(suite, ["env_1"], model_cfgs)

        # Single environment, clean checkout -> passes
        mock_sub.return_value = MagicMock(returncode=0, stdout="")
        res = check_experiment_freeze(suite, ["env_1"], model_cfgs)
        assert res == "B"


def _summary(run_dir: str, model: str = "m", commit: str = "abc123", **manifest) -> dict:
    base = {
        "contract_version": "c1",
        "task_set_version": "t1",
        "grader_version": "g1",
        "inference_config": {"temperature": None},
        "env_mode": "live",
        "experiment_type": "B",
        "uv_lock_hash": "u1",
        "trials": 1,
        "runtime": {},
    }
    base.update(manifest)
    return {
        "run_dir": run_dir,
        "model": model,
        "models": [model],
        "case_ids": ["c1"],
        "cases_scored": 1,
        "condor": {"commit": commit, "dirty_files": 0},
        "manifest": base,
    }


def test_compare_allows_a_model_only_diff():
    """Experiment B: the model is the variable under test."""
    comp = compare_summaries([_summary("r1", "m1"), _summary("r2", "m2")])
    assert comp["comparable"] is True
    assert comp["variable_under_test"] == "model"
    assert comp["deltas"] is not None


def test_compare_counts_inference_config_with_the_model():
    """B lets each model use its own params — model + inference is one variable."""
    comp = compare_summaries(
        [
            _summary("r1", "m1"),
            _summary("r2", "m2", inference_config={"temperature": 0.2}),
        ]
    )
    assert comp["comparable"] is True
    assert comp["variable_under_test"] == "model"


def test_compare_refuses_model_and_checkout_together():
    comp = compare_summaries(
        [_summary("r1", "m1", commit="aaa"), _summary("r2", "m2", commit="bbb")]
    )
    assert comp["comparable"] is False
    assert comp["differences"] == ["model", "condor_checkout"]
    assert comp["deltas"] is None


def test_compare_refuses_on_dirty_vs_clean_checkout_plus_model():
    a = _summary("r1", "m1")
    b = _summary("r2", "m2")
    b["condor"]["dirty_files"] = 3
    comp = compare_summaries([a, b])
    assert comp["comparable"] is False
    assert "condor_checkout" in comp["differences"]


def test_compare_still_refuses_partial_groups():
    a, b = _summary("r1"), _summary("r2")
    b["member_status"] = "failed"
    comp = compare_summaries([a, b])
    assert comp["comparable"] is False
    assert comp["differences"] == ["partial_group"]


# ── Inference config reaches the model ─────────────────────────────────────────
def test_inference_model_settings_only_carry_set_fields(monkeypatch: pytest.MonkeyPatch):
    for key in ("BENCH_TEMPERATURE", "BENCH_TOP_P", "BENCH_MAX_TOKENS", "BENCH_REASONING", "BENCH_SEED"):
        monkeypatch.delenv(key, raising=False)
    assert inference_config().model_settings() == {}

    monkeypatch.setenv("BENCH_TEMPERATURE", "0")
    monkeypatch.setenv("BENCH_REASONING", "false")
    monkeypatch.setenv("BENCH_SEED", "7")
    assert inference_config().model_settings() == {
        "temperature": 0.0,
        "seed": 7,
        "thinking": False,
    }


def test_inference_config_rejects_unparsable_values(monkeypatch: pytest.MonkeyPatch):
    """A typo must not quietly become "provider default" in the manifest."""
    monkeypatch.setenv("BENCH_TEMPERATURE", "0,2")
    with pytest.raises(ValueError, match="BENCH_TEMPERATURE"):
        inference_config()


def test_make_client_passes_pinned_settings_to_pydantic_ai(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BENCH_TEMPERATURE", "0.1")
    monkeypatch.setenv("BENCH_MAX_TOKENS", "2048")
    client, is_acp = _make_client("anthropic:claude-sonnet-5", [])
    assert is_acp is False
    assert client.model_settings == {"temperature": 0.1, "max_tokens": 2048}


def test_manifest_records_the_settings_the_client_applies(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BENCH_TOP_P", "0.9")
    pin = build_manifest(case_ids=["c1"], models=["m"])
    assert pin["manifest"]["inference_config"]["top_p"] == 0.9
    assert InferenceConfig(top_p=0.9).model_settings() == {"top_p": 0.9}


# ── Freeze checks ──────────────────────────────────────────────────────────────
def test_experiment_a_refuses_per_model_inference_drift():
    suite = {"id": "s", "experiment_type": "A"}
    cfgs = [
        {"key": "anthropic:claude-sonnet-5"},
        {"key": "anthropic:claude-sonnet-5", "inference_config": {"temperature": 0.7}},
    ]
    with pytest.raises(FreezeViolationError, match="different inference configs"):
        check_experiment_freeze(suite, ["e1", "e2"], cfgs)


def test_experiment_a_honours_the_preset_pins(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("BENCH_TEMPERATURE", raising=False)
    suite = {
        "id": "s",
        "experiment_type": "A",
        "freeze": {"model_id": "anthropic:claude-sonnet-5", "inference_config": {"temperature": 0.0}},
    }
    with pytest.raises(FreezeViolationError, match="preset pins model"):
        check_experiment_freeze(suite, ["e1"], [{"key": "openai:gpt-4o"}])
    with pytest.raises(FreezeViolationError, match="differs from the preset pin"):
        check_experiment_freeze(suite, ["e1"], [{"key": "anthropic:claude-sonnet-5"}])
    monkeypatch.setenv("BENCH_TEMPERATURE", "0")
    assert check_experiment_freeze(suite, ["e1"], [{"key": "anthropic:claude-sonnet-5"}]) == "A"


def test_unknown_experiment_type_is_refused():
    with pytest.raises(FreezeViolationError, match="Unknown experiment type"):
        check_experiment_freeze({"experiment_type": "a"}, ["e1"], [{"key": "m"}])


def _git_repo(path: Path) -> str:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=path,
        check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_experiment_b_refuses_a_commit_that_does_not_match_the_pin(tmp_path: Path):
    head = _git_repo(tmp_path)
    suite = {"id": "s", "experiment_type": "B"}
    cfgs = [{"key": "m1"}, {"key": "m2"}]
    env = {"condor_path": str(tmp_path), "expected_commit": "deadbeef"}
    with patch("bench.suite_runner.get_environment", return_value=env):
        with pytest.raises(FreezeViolationError, match="but the pin is deadbeef"):
            check_experiment_freeze(suite, ["e1"], cfgs)
        env["expected_commit"] = head[:8]
        assert check_experiment_freeze(suite, ["e1"], cfgs) == "B"
        # The preset's pin wins over the environment's.
        suite["freeze"] = {"condor_commit": "0000000"}
        with pytest.raises(FreezeViolationError, match="but the pin is 0000000"):
            check_experiment_freeze(suite, ["e1"], cfgs)


def test_experiment_b_refuses_a_checkout_it_cannot_inspect(tmp_path: Path):
    suite = {"id": "s", "experiment_type": "B"}
    with patch("bench.suite_runner.get_environment", return_value={"condor_path": str(tmp_path / "nope")}):
        with pytest.raises(FreezeViolationError, match="does not exist"):
            check_experiment_freeze(suite, ["e1"], [{"key": "m"}])
    with patch("bench.suite_runner.get_environment", return_value={"condor_path": str(tmp_path)}):
        with pytest.raises(FreezeViolationError, match="cannot read git state"):
            check_experiment_freeze(suite, ["e1"], [{"key": "m"}])


# ── Ad-hoc runs stay out of the frontier ───────────────────────────────────────
def test_adhoc_dashboard_and_suite_runs_do_not_feed_the_matrix():
    from config import summary_counts_for_matrix

    adhoc = {"manifest": {"experiment_type": "adhoc"}}
    # CLI test/sweep: the documented way to build the matrix.
    assert summary_counts_for_matrix({"run_type": "adhoc", **adhoc}) is True
    # Dashboard runs without a preset.
    assert summary_counts_for_matrix({"run_type": "adhoc", "source": "dashboard", **adhoc}) is False
    # Ad-hoc suites, even when they opt in.
    assert (
        summary_counts_for_matrix(
            {"run_type": "suite", "suite_id": "s", "include_in_matrix": True, **adhoc}
        )
        is False
    )
    # A preset-backed suite that opts in still counts.
    assert (
        summary_counts_for_matrix(
            {
                "run_type": "suite",
                "suite_id": "s",
                "include_in_matrix": True,
                "manifest": {"experiment_type": "B"},
            }
        )
        is True
    )
    # Pre-manifest dashboard summaries keep their old treatment.
    assert summary_counts_for_matrix({"run_type": "adhoc", "source": "dashboard"}) is True
