"""Suite / environment store, compare gates, and matrix exclusion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.compare import compare_summaries
from bench.matrix import build_matrix, load_runs
from config import summary_counts_for_matrix


def test_summary_counts_for_matrix_excludes_suite_and_custom():
    assert summary_counts_for_matrix({"run_type": "adhoc"}) is True
    assert summary_counts_for_matrix({"run_type": "suite"}) is False
    assert summary_counts_for_matrix({"suite_id": "x"}) is False
    assert summary_counts_for_matrix({"run_type": "custom_prompt"}) is False
    assert summary_counts_for_matrix({"run_type": "custom-prompt"}) is False
    assert summary_counts_for_matrix({"run_type": "suite", "include_in_matrix": True}) is True


def test_matrix_skips_suite_runs(tmp_path: Path, registry: Path):
    results = tmp_path / "results"
    # Ad-hoc run should count
    adhoc = results / "aaa_model"
    (adhoc / "cases").mkdir(parents=True)
    (adhoc / "summary.json").write_text(
        json.dumps(
            {
                "model": "ollama:small:3b",
                "timestamp": "2026-08-09T00:00:00Z",
                "run_type": "adhoc",
            }
        )
    )
    (adhoc / "cases" / "c1.json").write_text(
        json.dumps(
            {
                "case_id": "c1",
                "domain": "general_consult",
                "composite": 0.9,
                "risk_level": "read_only",
            }
        )
    )
    # Suite run must not count
    suite = results / "bbb_model"
    (suite / "cases").mkdir(parents=True)
    (suite / "summary.json").write_text(
        json.dumps(
            {
                "model": "ollama:small:3b",
                "timestamp": "2026-08-09T01:00:00Z",
                "run_type": "suite",
                "suite_id": "canary",
            }
        )
    )
    (suite / "cases" / "c2.json").write_text(
        json.dumps(
            {
                "case_id": "c2",
                "domain": "general_consult",
                "composite": 0.1,
                "risk_level": "read_only",
            }
        )
    )

    runs = load_runs(results)
    assert len(runs) == 1
    assert runs[0].run_dir == "aaa_model"

    matrix = build_matrix(results_dir=results, models_path=registry)
    cell = matrix["domains"]["general_consult"]["ollama:small:3b"]
    assert cell["pass_rate"] == 1.0


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    path = tmp_path / "models.json"
    path.write_text(
        json.dumps(
            {
                "models": [
                    {"key": "ollama:small:3b", "params_b": 3, "provider": "local"},
                ]
            }
        )
    )
    return path


def test_suite_store_crud_and_namespaced_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import bench.suites as suites

    monkeypatch.setattr(suites, "SUITES_DIR", tmp_path / "suites")
    monkeypatch.setattr(suites, "ENVIRONMENTS_DIR", tmp_path / "suites" / "environments")

    env = suites.create_environment(
        {
            "id": "condor-main",
            "name": "main",
            "condor_path": "/tmp/fake-condor",
            "require_clean": False,
        }
    )
    assert env["version"] == 1

    suite = suites.create_suite(
        {
            "id": "canary",
            "name": "Canary",
            "environment_ids": ["condor-main"],
            "models": [{"model_key": "anthropic:claude-haiku-4-5"}],
        }
    )
    imported = suites.import_library_cases(
        "canary", case_ids=["c001"], expected_version=suite["version"]
    )
    assert len(imported) == 1
    assert imported[0]["id"].startswith("canary__")
    assert imported[0]["source_case_id"] == "c001"

    suite2 = suites.get_suite("canary")
    prompts = suites.suite_prompt_map("canary")
    assert imported[0]["id"] in prompts
    assert prompts[imported[0]["id"]]

    with pytest.raises(suites.VersionConflict):
        suites.create_suite_case(
            "canary",
            {"type": "consult", "question": "hi", "version": 1},
            expected_version=1,
        )

    suites.create_suite_case(
        "canary",
        {"type": "consult", "question": "hello world"},
        expected_version=suite2["version"],
    )
    cases = suites.list_suite_cases("canary")
    assert any(c["question"] == "hello world" for c in cases)


def test_compare_multi_env_is_comparable_and_yields_deltas():
    """Two members differing only by Condor checkout is the A/B compare exists for."""
    members = [
        {
            "run_dir": "a",
            "models": ["m"],
            "model": "m",
            "case_ids": ["x"],
            "pass_rate": 0.5,
            "composite_avg": 0.5,
            "latency_s_avg": 2.0,
            "cases_scored": 1,
            "environment_id": "e1",
            "usage": {"avg_total_tokens": 100},
        },
        {
            "run_dir": "b",
            "models": ["m"],
            "model": "m",
            "case_ids": ["x"],
            "pass_rate": 0.6,
            "composite_avg": 0.6,
            "latency_s_avg": 1.5,
            "cases_scored": 1,
            "environment_id": "e2",
            "usage": {"avg_total_tokens": 90},
        },
    ]
    result = compare_summaries(members)
    assert result["comparable"] is True
    assert result["differences"] == []
    assert result["deltas"] is not None
    assert result["deltas"]["pairs"][0]["latency_n"]["baseline"] == 1


def test_build_run_pin_paths_are_strings():
    from config import build_run_pin

    pin = build_run_pin(run_type="adhoc", case_ids=["c001"], models=["m"])
    # Must be JSON-serializable
    json.dumps(pin)
    condor = pin["condor"]
    assert condor["path"] is None or isinstance(condor["path"], str)
    for key in ("shared_py", "config_yml", "acp_working_dir", "sys_path_head"):
        val = condor["loaded"][key]
        assert val is None or isinstance(val, str)
