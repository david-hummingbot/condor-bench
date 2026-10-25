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


# ── Model catalogue for the suite pickers ──────────────────────────────────────
def test_the_model_catalog_is_live_where_it_can_be_and_says_why_not(client, monkeypatch):
    import dashboard.backend.app as A

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-secretvalue1234")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    calls = []

    async def fake_list(base_url, api_key="", provider="", *, timeout=10):
        calls.append((provider, api_key))
        if provider == "openrouter":
            return ["openai/gpt-4o-mini", "deepseek/deepseek-chat"]
        if provider == "lmstudio":
            return ["qwen2.5-7b-instruct"]
        raise ConnectionError("refused")

    monkeypatch.setattr(A, "_list_models", fake_list)
    A._CATALOG_CACHE.update(at=0.0, data=None)
    groups = {g["provider"]: g for g in client.get("/api/model-catalog").json()["groups"]}

    assert list(groups)[0] == "registry", "the registry comes first"
    assert groups["openrouter"]["source"] == "live"
    assert "openrouter:deepseek/deepseek-chat" in groups["openrouter"]["models"]
    assert ("openrouter", "sk-or-v1-secretvalue1234") in calls, "the Settings key is used"
    assert groups["groq"]["source"] == "built-in" and "add a key in Settings" in groups["groq"]["note"]
    assert not any(p == "groq" for p, _ in calls), "no key, no call"
    assert "not reachable" in groups["ollama"]["note"]
    assert groups["lmstudio"]["models"] == ["lmstudio:qwen2.5-7b-instruct"]
    assert "claude-code:sonnet" in groups["claude-code"]["models"]
    assert "secretvalue" not in client.get("/api/model-catalog").text

    # Cached: a second load does not refetch; refresh does.
    n = len(calls)
    client.get("/api/model-catalog")
    assert len(calls) == n
    client.get("/api/model-catalog", params={"refresh": 1})
    assert len(calls) > n
    A._CATALOG_CACHE.update(at=0.0, data=None)


def test_the_dashboard_refuses_cross_origin_and_rebound_requests():
    """Any web page could read saved API keys with one fetch while CORS allowed
    every origin; a rebound DNS name reaches it without an Origin at all."""
    from fastapi.testclient import TestClient

    from dashboard.backend.app import app

    client = TestClient(app)
    assert client.get("/api/form-state").status_code == 200
    assert client.get("/api/form-state", headers={"origin": "http://localhost:5173"}).status_code == 200
    evil = client.get("/api/form-state", headers={"origin": "https://evil.example"})
    assert evil.status_code == 403 and "access-control-allow-origin" not in evil.headers
    assert client.get("/api/form-state", headers={"host": "evil.example"}).status_code == 403
    assert client.post("/api/compare-condor/prepare", json={"folder": "/tmp/x"},
                       headers={"origin": "https://evil.example"}).status_code == 403


def test_a_custom_prompt_is_bounded_and_undoes_what_it_created(monkeypatch):
    """An ad-hoc "buy 0.01 BTC" ran with no ceiling and no teardown, leaving its
    executor to block the next run's pre-flight."""
    import asyncio
    from types import SimpleNamespace

    import bench.client
    import bench.cleanup
    import bench.staging_health
    import config
    from dashboard.backend import app as appmod

    async def ready():
        return None

    async def placed_an_order_then_hung(*a, **k):
        call = {"tool": "create_order_executor", "tool_call_id": "t1", "args": {}}
        bench.client._PARTIAL_TRACE.get().turns.append(
            ([call], [{"tool_call_id": "t1", "output": '{"executor_id": "ex1"}'}]))
        await asyncio.sleep(10)

    torn: list = []

    async def fake_teardown(result, model, **kw):
        torn.append([c["tool"] for c in result.tool_calls])
        return SimpleNamespace(resources=[])

    monkeypatch.setattr(bench.staging_health, "a_assert_ready", ready)
    monkeypatch.setattr(bench.client, "run_consult", placed_an_order_then_hung)
    monkeypatch.setattr(bench.cleanup, "teardown", fake_teardown)
    monkeypatch.setattr(config, "case_timeout_s", lambda _b=None: 0.05)

    run_id = "cptest01"
    appmod._custom_runs[run_id] = {"run_id": run_id, "status": "starting", "events": [], "listeners": []}
    try:
        req = appmod.CustomPromptRequest(question="buy 0.01 BTC",
                                         models=[appmod.ModelConfig(model_key="openrouter:x/y")])
        asyncio.run(appmod._run_custom_prompt(run_id, req))
        done = [e for e in appmod._custom_runs[run_id]["events"] if e["type"] == "model_done"]
        assert "timed out" in done[0]["error"]
        assert torn == [["create_order_executor"]]
    finally:
        appmod._custom_runs.pop(run_id, None)


def test_runs_of_any_kind_do_not_overlap():
    from fastapi import HTTPException

    from dashboard.backend import app as appmod

    appmod._custom_runs["busy0001"] = {"status": "running"}
    try:
        with pytest.raises(HTTPException) as exc:
            appmod._refuse_if_busy()
        assert exc.value.status_code == 409
    finally:
        appmod._custom_runs.pop("busy0001", None)


def test_a_custom_prompt_can_be_cancelled():
    """The run guard told the user to cancel a custom prompt that had no cancel."""
    import asyncio

    from dashboard.backend import app as appmod

    async def go():
        task = asyncio.create_task(asyncio.sleep(30))
        appmod._custom_runs["cancel01"] = {"status": "running", "task": task}
        try:
            assert (await appmod.cancel_custom_prompt("cancel01")) == {"status": "cancelling"}
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            appmod._custom_runs.pop("cancel01", None)

    asyncio.run(go())


def test_validating_an_environment_runs_off_the_event_loop(client, monkeypatch):
    """The probe is a `uv run` subprocess of up to 60s, called synchronously from an
    async endpoint: SSE, cancel and pause all stalled behind it, and a timeout was
    an unhandled 500."""
    import asyncio
    import subprocess

    import bench.suites as suites

    seen = []

    def probe(env_id):
        try:
            asyncio.get_running_loop()
            seen.append("on the event loop")
        except RuntimeError:
            seen.append("in a worker thread")
        return {"ok": True, "environment_id": env_id}

    monkeypatch.setattr(suites, "validate_environment", probe)
    assert client.get("/api/environments/e1/validate").json()["ok"] is True
    assert seen == ["in a worker thread"]

    def slow(env_id):
        raise subprocess.TimeoutExpired(["uv", "run"], 60)

    monkeypatch.setattr(suites, "validate_environment", slow)
    r = client.get("/api/environments/e1/validate")
    assert r.status_code == 504 and "did not finish in 60s" in r.json()["detail"]


def test_compare_refuses_unknown_and_out_of_tree_run_dirs(client, monkeypatch, tmp_path):
    """An unknown run dir was a 500, and `../` read any summary.json on disk."""
    import json

    import bench.compare as compare

    results = tmp_path / "results"
    (results / "r1").mkdir(parents=True)
    (results / "r1" / "summary.json").write_text(json.dumps({"model": "m"}))
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "summary.json").write_text(json.dumps({"model": "leak"}))
    monkeypatch.setattr(compare, "RESULTS_DIR", results)

    for runs in ("r1,nope", "r1,../secret", f"r1,{tmp_path / 'secret'}", "r1,.", "r1,.."):
        r = client.get("/api/compare", params={"runs": runs})
        assert r.status_code == 404, (runs, r.status_code)
    with pytest.raises(FileNotFoundError):
        compare.load_summary("../secret")
    assert compare.load_summary("r1")["run_dir"] == "r1"
