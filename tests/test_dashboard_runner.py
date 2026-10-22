"""The dashboard runs cases through the CLI's trial runner, and shows what it records."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


class _Card:
    def __init__(self, cid, trial=1):
        self.case_id = cid
        self.trial_idx = trial
        self.composite = 0.9
        self.harness_artifact = None
        self.forbidden_violations = []
        self.usage = {}
        self.latency_s = 1.0

    def as_dict(self):
        return {"case_id": self.case_id, "trial_idx": self.trial_idx, "composite": self.composite,
                "harness_artifact": self.harness_artifact}


def _stub_run(monkeypatch, cases, *, crash_on=None):
    """Stub everything that would touch a target; record what the run saved and emitted."""
    import bench.baseline
    import bench.cleanup
    import bench.client
    import bench.dataset
    import bench.market_resolver
    import bench.market_warmup
    import bench.mcp_provider
    import bench.probe_journal
    import bench.reporter
    import bench.scorer
    import bench.staging_health

    saved: dict = {}

    async def fake_run_case(case, model):
        if case.id == crash_on:
            raise RuntimeError("bridge died")
        return SimpleNamespace(latency_s=1.0, response="ok")

    async def fake_score(case, result, baseline, *, trial_idx=1):
        return _Card(case.id, trial_idx)

    def fake_save(model, scorecards, responses, run_id, *, prompts=None, extra_summary=None):
        saved["scorecards"] = list(scorecards)
        saved["summary"] = dict(extra_summary or {})
        from pathlib import Path

        return Path("results") / "dash_run"

    async def fake_prepare(cs):
        return list(cs), {}

    async def ok_warmup(case):
        return SimpleNamespace(ok=True, notes=[])

    async def ready(*a, **k):
        return None

    monkeypatch.setattr(bench.staging_health, "a_assert_ready", ready)
    monkeypatch.setattr(bench.dataset, "load_all_cases", lambda *a, **k: cases)
    monkeypatch.setattr(bench.dataset, "case_prompt_map", lambda *a, **k: {})
    monkeypatch.setattr(bench.dataset, "is_mutating", lambda case: False)
    monkeypatch.setattr(bench.client, "run_case", fake_run_case)
    monkeypatch.setattr(bench.client, "case_input_text", lambda case: "q")
    monkeypatch.setattr(bench.scorer, "score_case", fake_score)
    monkeypatch.setattr(bench.reporter, "save_run", fake_save)
    monkeypatch.setattr(bench.mcp_provider, "target_banner", lambda: "test")
    monkeypatch.setattr(bench.baseline, "BaselineStore", lambda *a, **k: SimpleNamespace(load=lambda _: None))
    monkeypatch.setattr(bench.cleanup, "created_resources", lambda r: [])
    monkeypatch.setattr(bench.market_resolver, "prepare_cases", fake_prepare)
    monkeypatch.setattr(bench.market_resolver, "bindings_summary", lambda r: {})
    monkeypatch.setattr(bench.market_warmup, "ensure_markets_for_case", ok_warmup)
    monkeypatch.setattr(bench.probe_journal, "ensure_probe_journal", lambda case: None)
    return saved


def _case(cid):
    return SimpleNamespace(
        id=cid, type="consult", domain="general_consult", risk_level="read_only",
        category="x", agent_slug=None, tags=[], lanes=[], mock_faults=[], expected_tools=[],
        task={"split": "regression", "category": "market_reads", "task_id": f"T-reads-{cid}", "version": "1"},
    )


def _run(req_kwargs):
    from dashboard.backend import app as A

    run_id = "dashtest"
    A._active_runs[run_id] = {
        "run_id": run_id, "status": "starting", "events": [], "listeners": [],
        "task": None, "total": 0, "current_case": None, "next_case": None,
        "pause_event": asyncio.Event(),
    }
    try:
        asyncio.run(A._run_benchmark(run_id, A.RunRequest(models=[{"model_key": "ollama:small:3b"}], **req_kwargs)))
        return dict(A._active_runs[run_id])
    finally:
        A._active_runs.clear()


def test_a_dashboard_run_does_every_trial_and_records_crashes(monkeypatch):
    monkeypatch.delenv("BENCH_ENV_MODE", raising=False)
    saved = _stub_run(monkeypatch, [_case("a"), _case("b")], crash_on="b")
    state = _run({"trials": 2})

    assert state["status"] == "completed", state
    rows = [(sc.case_id, sc.trial_idx) for sc in saved["scorecards"]]
    assert rows == [("a", 1), ("a", 2), ("b", 1), ("b", 2)], (
        "every trial is a row, and a crashed trial is recorded rather than dropped"
    )
    crashed = [sc for sc in saved["scorecards"] if sc.case_id == "b"]
    assert all("bridge died" in sc.harness_artifact for sc in crashed)
    assert saved["summary"]["manifest"]["trials"] == 2
    assert saved["summary"]["source"] == "dashboard"

    started = [e for e in state["events"] if e["type"] == "run_started"][0]
    assert started["total"] == 4 and started["trials"] == 2
    done = [(e["case_id"], e["trial"]) for e in state["events"] if e["type"] == "case_done"]
    assert done == rows


def test_the_dashboard_never_runs_holdout(monkeypatch):
    _stub_run(monkeypatch, [_case("a")])
    state = _run({"splits": ["holdout"]})
    assert state["status"] == "failed"
    assert "holdout" in [e for e in state["events"] if e["type"] == "run_done"][0]["error"]


def test_a_mock_only_case_is_skipped_on_live_and_noted(monkeypatch):
    monkeypatch.delenv("BENCH_ENV_MODE", raising=False)
    mock_only = _case("m")
    mock_only.lanes = ["mock"]
    saved = _stub_run(monkeypatch, [_case("a"), mock_only])
    state = _run({})
    assert [sc.case_id for sc in saved["scorecards"]] == ["a"]
    notes = [e["text"] for e in state["events"] if e["type"] == "note"]
    assert any("need another lane" in n and "m" in n for n in notes)


def test_the_lane_is_restored_after_a_run(monkeypatch):
    monkeypatch.setenv("BENCH_ENV_MODE", "live")
    import bench.lanes

    monkeypatch.setattr(bench.lanes, "ensure_mock_running",
                        lambda: SimpleNamespace(api_url="http://m", started=False))
    import bench.trials

    monkeypatch.setattr(bench.trials, "seed_mock", lambda *a, **k: None)
    monkeypatch.setattr(bench.trials, "flag_mock_gaps", lambda card: None)
    _stub_run(monkeypatch, [_case("a")])
    state = _run({"env_mode": "mock"})
    assert state["status"] == "completed", state
    import os

    assert os.environ["BENCH_ENV_MODE"] == "live"


# ── Endpoints ──────────────────────────────────────────────────────────────────
@pytest.fixture
def client():
    from dashboard.backend.app import app

    return TestClient(app)


def test_dataset_combos_carry_split_category_and_lane(client):
    data = client.get("/api/datasets").json()
    combo = data["combos"][0]
    assert {"split", "task_category", "lanes"} <= set(combo)
    assert set(data["splits"]) <= {"dev", "regression"}, "holdout is not part of the library"
    assert "negative_safety" in data["task_categories"]
    assert any(c["lanes"] == ["mock"] for c in data["combos"]), "the mock-only safety cases"


def test_the_nvidia_provider_offers_only_smoke_tested_registry_models(client):
    providers = {p["id"]: p for p in client.get("/api/providers").json()["providers"]}
    nim = providers["nvidia"]
    assert nim["api_base"] == "https://integrate.api.nvidia.com/v1"
    assert "openai/gpt-oss-20b" in nim["models"]
    assert not nim.get("fetch_models"), "the catalogue lists models an account cannot call"


def test_a_suite_run_with_a_bad_preset_is_refused_up_front(client, monkeypatch, tmp_path):
    import bench.suites as suites

    monkeypatch.setattr(suites, "SUITES_DIR", tmp_path / "suites")
    monkeypatch.setattr(suites, "ENVIRONMENTS_DIR", tmp_path / "envs")
    suites.create_suite({"id": "s1"})
    r = client.post("/api/suites/s1/run", json={"preset": "nope"})
    assert r.status_code == 400 and "No preset" in r.json()["detail"]
    r = client.post("/api/suites/s1/run", json={"preset": "a_example", "adhoc": True})
    assert r.status_code == 400 and "adhoc" in r.json()["detail"]


def test_the_form_remembers_lane_trials_split_and_category():
    from bench.form_state import FormStateError, _clean_benchmark

    clean = _clean_benchmark({"splits": ["regression"], "taskCategories": ["negative_safety"],
                              "trials": 3, "envMode": "mock"})
    assert clean["trials"] == 3 and clean["envMode"] == "mock"
    for bad in ({"splits": ["holdout"]}, {"trials": 0}, {"envMode": "staging"},
                {"taskCategories": ["nope"]}):
        with pytest.raises(FormStateError):
            _clean_benchmark(bad)


# ── Settings keys in the run form ──────────────────────────────────────────────
def test_the_run_form_sees_a_settings_key_masked_never_raw(client, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-secretvalue1234")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    providers = {p["id"]: p for p in client.get("/api/providers").json()["providers"]}
    assert providers["openrouter"]["settings_key"] == "••••1234"
    assert providers["groq"]["settings_key"] is None
    assert providers["ollama"]["settings_key"] is None, "local providers take no key"
    assert "secretvalue" not in client.get("/api/providers").text


def test_a_settings_key_only_goes_to_that_providers_own_endpoint(monkeypatch):
    from dashboard.backend.app import _settings_key_for

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-secretvalue1234")
    assert _settings_key_for("https://openrouter.ai/api/v1", "openrouter") == "sk-or-v1-secretvalue1234"
    assert _settings_key_for("https://openrouter.ai/api/v1/", "openrouter") == "sk-or-v1-secretvalue1234"
    # A caller-chosen URL never gets the install's key.
    assert _settings_key_for("https://evil.example/v1", "openrouter") == ""
    assert _settings_key_for("https://openrouter.ai/api/v1", "custom") == ""


def test_model_listing_falls_back_to_the_settings_key(client, monkeypatch):
    import httpx

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-secretvalue1234")
    seen = {}

    async def fake_get(self, url, headers=None, **kw):
        seen["auth"] = (headers or {}).get("Authorization")
        return httpx.Response(200, json={"data": [{"id": "openai/gpt-4o-mini"}]},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    r = client.get("/api/provider-models", params={"base_url": "https://openrouter.ai/api/v1",
                                                   "provider": "openrouter"})
    assert r.status_code == 200 and "openai/gpt-4o-mini" in r.json()["models"]
    assert seen["auth"] == "Bearer sk-or-v1-secretvalue1234"

    r = client.get("/api/provider-models", params={"base_url": "https://evil.example/v1",
                                                   "provider": "openrouter"})
    assert seen["auth"] is None, "an unknown URL got the saved key"
