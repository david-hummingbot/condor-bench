"""`runner.py hub` end to end, with the network, staging and model stubbed out."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

import runner
from bench import hub_agents as hub
from tests.test_hub_agents import _zip


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Everything `hub` touches outside bench, replaced; returns what it saw."""
    import bench.matrix
    import bench.reporter
    import bench.staging_health

    seen: dict = {"saved": [], "reset": [], "ran": []}
    monkeypatch.setenv("CONDOR_AGENTS_ROOT", str(tmp_path / "agents"))
    bundle = hub.parse_zip(_zip(), slug="test_trader")
    monkeypatch.setattr(hub, "load_agent", lambda source: bundle)
    monkeypatch.setattr(hub, "surface_tools", lambda: [])
    monkeypatch.setattr(hub, "skills_index", lambda slug: "- [sizing] how to size")
    monkeypatch.setattr(bench.matrix, "build_matrix", lambda **kw: {})
    monkeypatch.setattr(bench.staging_health, "assert_ready", lambda: None)
    monkeypatch.setattr(runner, "_enter_lane", lambda default: "live")
    monkeypatch.setattr(runner, "_print_summary", lambda *a, **kw: None)

    class _Reset:
        def as_dict(self):
            return {"archived": True}

    def reset(model, run_id):
        seen["reset"].append(model)
        return _Reset()

    async def run_cases(cases, model, store, trials):
        seen["ran"].append((model, [c.id for c in cases], [c.timeout_s for c in cases]))
        assert (tmp_path / "agents" / "bench_hub_test_trader" / "AGENT.md").is_file(), \
            "cases run against the installed agent"
        if seen.get("fail"):
            raise RuntimeError("model endpoint fell over")
        return [], {}

    def save_run(model, scorecards, responses, run_id, prompts=None, extra_summary=None):
        seen["saved"].append((model, extra_summary))
        return tmp_path / f"run_{model}"

    monkeypatch.setattr(runner, "_reset_acp_memory", reset)
    monkeypatch.setattr(runner, "_run_cases", run_cases)
    monkeypatch.setattr(bench.reporter, "save_run", save_run)
    seen["home"] = tmp_path / "agents" / "bench_hub_test_trader"
    return seen


def _invoke(*args):
    return CliRunner().invoke(runner.app, ["hub", "hub:test_trader", *args])


def test_each_model_runs_cold_and_is_saved_as_a_hub_run(wired):
    out = _invoke("-m", "m:a", "-m", "m:b", "--timeout", "300")
    assert out.exit_code == 0, out.output
    assert wired["reset"] == ["m:a", "m:b"], "every model starts without its last run's memory"
    assert [m for m, _, _ in wired["ran"]] == ["m:a", "m:b"]
    assert set(wired["ran"][0][2]) == {300}
    for _, summary in wired["saved"]:
        assert summary["run_type"] == "hub_agent"
        assert summary["hub_agent"]["installed_as"] == "bench_hub_test_trader"
        assert summary["agent_memory_reset"] == {"archived": True}
    assert not wired["home"].exists(), "uninstalled after the run"


def test_a_failed_run_still_uninstalls(wired):
    wired["fail"] = True
    out = _invoke("-m", "m:a")
    assert out.exit_code != 0
    assert wired["ran"] and not wired["saved"]
    assert not wired["home"].exists(), "the install must not outlive a crashed run"


@pytest.mark.parametrize("bad", ["0", "-5", "601"])
def test_a_timeout_outside_the_harness_range_is_refused(wired, bad):
    out = _invoke("-m", "m:a", "--timeout", bad)
    assert out.exit_code == 2
    assert not wired["ran"] and not wired["home"].exists()


def test_no_model_estimates_without_installing(wired):
    out = _invoke()
    assert out.exit_code == 0, out.output
    assert not wired["ran"] and not wired["home"].exists()
