"""Phase 3: the mock lane — the stand-in API, lane selection, and lane isolation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from mock_mcp.server import MockState, create_app


# ── The mock hummingbot-api ────────────────────────────────────────────────────
def _call(app, method: str, path: str, *, auth=("mock", "mock"), **kw) -> httpx.Response:
    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mock", auth=auth) as c:
            return await c.request(method, path, **kw)

    return asyncio.run(go())


@pytest.fixture
def mock(monkeypatch):
    monkeypatch.delenv("BENCH_MOCK_USERNAME", raising=False)
    monkeypatch.delenv("BENCH_MOCK_PASSWORD", raising=False)
    return create_app(MockState())


def test_prices_come_from_the_fixed_snapshot(mock):
    r = _call(mock, "POST", "/market-data/prices",
              json={"connector_name": "binance", "trading_pairs": ["BTC-USDT", "NOPE-USDT"]})
    assert r.status_code == 200
    assert r.json()["prices"] == {"BTC-USDT": 65000.0}


def test_executor_lifecycle_is_stateful(mock):
    """The anti-canned-payload property: what a model does is what the state shows."""
    assert _call(mock, "POST", "/executors/search", json={}).json()["data"] == []
    created = _call(mock, "POST", "/executors/", json={"executor_config": {
        "type": "order_executor", "connector_name": "binance", "trading_pair": "BTC-USDT", "side": 1}})
    executor_id = created.json()["executor_id"]

    running = _call(mock, "POST", "/executors/search", json={"status": "RUNNING"}).json()["data"]
    assert [e["executor_id"] for e in running] == [executor_id]
    assert _call(mock, "GET", "/executors/summary").json()["total_active"] == 1

    assert _call(mock, "POST", f"/executors/{executor_id}/stop", json={}).json()["status"] == "stopped"
    again = _call(mock, "POST", f"/executors/{executor_id}/stop", json={}).json()
    assert again["status"] == "already_terminated"
    assert _call(mock, "GET", "/executors/summary").json()["total_active"] == 0


def test_unknown_markets_are_refused_like_a_real_api(mock):
    r = _call(mock, "POST", "/executors/", json={"executor_config": {
        "type": "order_executor", "connector_name": "binance", "trading_pair": "DOGE-USDT"}})
    assert r.status_code == 400


def test_reset_returns_to_the_fixture(mock):
    _call(mock, "POST", "/executors/", json={"executor_config": {
        "connector_name": "binance", "trading_pair": "BTC-USDT"}})
    _call(mock, "POST", "/_mock/reset", json={})
    state = _call(mock, "GET", "/_mock/state").json()
    assert state["executors"] == [] and state["calls"] == []


def test_auth_is_enforced_on_the_api_but_not_the_control_plane(mock):
    assert _call(mock, "GET", "/accounts/", auth=("x", "y")).status_code == 401
    assert _call(mock, "GET", "/_mock/health", auth=None).status_code == 200


def test_unimplemented_endpoints_are_loud(mock):
    r = _call(mock, "GET", "/bot-orchestration/status")
    assert r.status_code == 501
    assert "does not implement" in r.json()["detail"]
    assert _call(mock, "GET", "/_mock/state").json()["unhandled"] == ["GET /bot-orchestration/status"]


@pytest.mark.parametrize(
    "kind,status", [("5xx", 503), ("rate_limit", 429)]
)
def test_transport_faults_fire_once_then_clear(mock, kind, status):
    _call(mock, "POST", "/_mock/fault", json={"kind": kind, "path": "/accounts/"})
    assert _call(mock, "GET", "/accounts/").status_code == status
    assert _call(mock, "GET", "/accounts/").status_code == 200


def test_insufficient_funds_drains_balances_and_refuses_create(mock):
    _call(mock, "POST", "/_mock/fault", json={"kind": "insufficient_funds", "times": 1})
    rows = _call(mock, "POST", "/portfolio/state", json={"account_names": ["master_account"],
                 "connector_names": ["binance"]}).json()["master_account"]["binance"]
    assert all(row["available_units"] == 0.0 for row in rows)
    r = _call(mock, "POST", "/executors/", json={"executor_config": {
        "connector_name": "binance", "trading_pair": "BTC-USDT"}})
    assert r.status_code == 400 and "Insufficient" in r.json()["detail"]


def test_stale_price_backdates_the_timestamp(mock):
    import time

    _call(mock, "POST", "/_mock/fault", json={"kind": "stale_price", "times": 0})
    body = _call(mock, "POST", "/market-data/prices",
                 json={"connector_name": "binance", "trading_pairs": ["BTC-USDT"]}).json()
    assert time.time() - body["timestamp"] > 3000


def test_unknown_fault_kinds_are_rejected(mock):
    assert _call(mock, "POST", "/_mock/fault", json={"kind": "meteor"}).status_code == 422


# ── Lane selection ─────────────────────────────────────────────────────────────
def test_legacy_commands_default_to_live(monkeypatch):
    from bench.lanes import resolve_lane

    monkeypatch.delenv("BENCH_ENV_MODE", raising=False)
    assert resolve_lane("live") == "live"


def test_mock_default_commands_stay_mock_without_an_explicit_live(monkeypatch):
    from bench.lanes import resolve_lane

    monkeypatch.delenv("BENCH_ENV_MODE", raising=False)
    assert resolve_lane("mock") == "mock"
    import os

    assert os.environ["BENCH_ENV_MODE"] == "mock", "pinned for subprocesses"


def test_live_on_a_mock_default_command_needs_the_staging_preflight(monkeypatch):
    import bench.staging_health as health
    from bench.lanes import LaneError, resolve_lane

    def failing():
        raise health.StagingUnhealthy("api_reachable: refused")

    monkeypatch.setenv("BENCH_ENV_MODE", "live")
    monkeypatch.setattr(health, "assert_ready", failing)
    with pytest.raises(LaneError, match="staging pre-flight failed"):
        resolve_lane("mock")

    monkeypatch.setattr(health, "assert_ready", lambda: None)
    assert resolve_lane("mock") == "live"


def test_a_typo_in_the_lane_is_an_error_not_live(monkeypatch):
    from config import env_mode

    monkeypatch.setenv("BENCH_ENV_MODE", "mokc")
    with pytest.raises(ValueError, match="BENCH_ENV_MODE"):
        env_mode()


def test_the_mock_lane_ignores_staging_settings(monkeypatch):
    from config import staging_config

    monkeypatch.setenv("BENCH_ENV_MODE", "mock")
    monkeypatch.setenv("HUMMINGBOT_API_URL", "https://real-staging.example")
    monkeypatch.setenv("BENCH_SERVER_NAME", "prod")
    monkeypatch.delenv("BENCH_MOCK_URL", raising=False)
    monkeypatch.delenv("BENCH_MOCK_PORT", raising=False)
    cfg = staging_config()
    assert cfg["api_url"] == "http://127.0.0.1:18000"
    assert cfg["server_name"] == "bench_mock"
    assert cfg["env_mode"] == "mock"


def test_live_registration_never_takes_a_server_name_override(monkeypatch):
    from bench.staging_setup import ensure_bench_server

    monkeypatch.delenv("BENCH_ENV_MODE", raising=False)
    monkeypatch.setenv("BENCH_SERVER_NAME", "someone_elses_server")
    result = ensure_bench_server(api_url="http://h:1", username="u", password="p", dry_run=True)
    assert "bench_staging" in result.detail


def test_mock_registration_uses_its_own_entry(monkeypatch):
    from bench.staging_setup import ensure_bench_server

    monkeypatch.setenv("BENCH_ENV_MODE", "mock")
    result = ensure_bench_server(dry_run=True)
    assert "bench_mock" in result.detail


def test_the_manifest_records_the_lane(monkeypatch):
    from bench.manifest import build_manifest

    monkeypatch.setenv("BENCH_ENV_MODE", "mock")
    assert build_manifest(case_ids=["c"], models=["m"])["manifest"]["env_mode"] == "mock"


# ── Lane isolation ─────────────────────────────────────────────────────────────
def _write_run(results: Path, name: str, manifest: dict | None) -> None:
    run_dir = results / name
    (run_dir / "cases").mkdir(parents=True)
    summary = {"model": "ollama:small:3b", "timestamp": f"2026-10-0{len(name) % 9 + 1}T00:00:00Z"}
    if manifest is not None:
        summary["manifest"] = manifest
    (run_dir / "summary.json").write_text(json.dumps(summary))
    (run_dir / "cases" / "c1.json").write_text(json.dumps({
        "case_id": "c1", "domain": "general_consult", "composite": 0.9, "passed": True,
        "latency_s": 1.0, "usage": {}, "risk_level": "read_only"}))


def test_mock_and_live_rows_never_share_a_matrix(tmp_path):
    from bench.matrix import load_runs

    results = tmp_path / "results"
    _write_run(results, "live_run", {"env_mode": "live"})
    _write_run(results, "mock_run", {"env_mode": "mock"})
    _write_run(results, "old_run", None)  # pre-manifest: live by construction
    _write_run(results, "unknown_run", {"experiment_type": "adhoc"})  # manifest, no lane

    assert sorted(r.run_dir for r in load_runs(results, lane="live")) == ["live_run", "old_run"]
    assert [r.run_dir for r in load_runs(results, lane="mock")] == ["mock_run"]


def test_each_lane_saves_its_own_matrix_and_routing(tmp_path, monkeypatch):
    import bench.matrix as matrix
    import bench.routing as routing

    monkeypatch.setattr(matrix, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(routing, "RESULTS_DIR", tmp_path)
    assert matrix.save_matrix({"env_mode": "mock"}).name == "matrix_mock.json"
    assert matrix.save_matrix({"env_mode": "live"}).name == "matrix.json"
    assert matrix.load_matrix(lane="mock") == {"env_mode": "mock"}
    assert routing.save_routing({"env_mode": "mock"}).name == "routing_recommendations_mock.json"
    assert routing.load_routing() is None, "no live routing was written"


def test_compare_refuses_to_difference_across_lanes():
    from bench.compare import compare_summaries

    def member(run_dir, lane):
        return {"run_dir": run_dir, "model": "m", "models": ["m"], "case_ids": ["c1"],
                "condor": {"commit": "abc"}, "manifest": {"env_mode": lane, "trials": 1}}

    comp = compare_summaries([member("a", "live"), member("b", "mock")])
    assert comp["comparable"] is False
    assert comp["differences"] == ["manifest:env_mode"]


# ── Infra policy ───────────────────────────────────────────────────────────────
def _trial(monkeypatch, *, reset_fails: int = 0, run_raises: bool = False):
    from types import SimpleNamespace

    import bench.client
    import bench.lanes
    import runner

    monkeypatch.setenv("BENCH_ENV_MODE", "mock")
    calls = {"reset": 0}

    def fake_reset(fixture=None):
        calls["reset"] += 1
        if calls["reset"] <= reset_fails:
            raise httpx.ConnectError("mock down")

    async def fake_run_case(case, model):
        raise RuntimeError("bridge died")

    monkeypatch.setattr(bench.lanes, "reset_mock", fake_reset)
    if run_raises:
        monkeypatch.setattr(bench.client, "run_case", fake_run_case)
    case = SimpleNamespace(id="c1", type="tool", risk_level="read_only", expected_tools=[])
    cards: list = []
    asyncio.run(runner._run_trial(case, "m", SimpleNamespace(load=lambda _: None), 2, cards, {}))
    return cards, calls


def test_a_trial_that_cannot_be_seeded_is_retried_then_recorded_as_infra(monkeypatch):
    cards, calls = _trial(monkeypatch, reset_fails=5)
    assert calls["reset"] == 2, "retried once"
    assert len(cards) == 1
    card = cards[0]
    assert card.harness_artifact and card.error.startswith("infra:")
    assert card.trial_idx == 2


def test_a_crashed_trial_is_recorded_not_dropped(monkeypatch):
    cards, _ = _trial(monkeypatch, run_raises=True)
    assert len(cards) == 1
    assert "bridge died" in cards[0].harness_artifact


def test_portfolio_overview_reads_answer_empty_not_501(mock):
    for path in ("/trading/positions", "/trading/orders/search", "/gateway/clmm/positions/search"):
        r = _call(mock, "POST", path, json={})
        assert r.status_code == 200 and r.json()["data"] == [], path


def test_candles_are_flat_at_the_fixture_price(mock):
    rows = _call(mock, "POST", "/market-data/candles", json={
        "connector_name": "binance", "trading_pair": "BTC-USDT", "interval": "1h", "max_records": 5}).json()
    assert len(rows) == 5
    assert {r["close"] for r in rows} == {65000.0}
    assert rows[1]["timestamp"] - rows[0]["timestamp"] == 3600


def test_a_timed_out_trial_still_undoes_what_it_created(monkeypatch):
    """A trial cut off by its ceiling never returns a result; teardown must work
    from the trace it got as far as recording. Before, a timed-out market buy left
    its executor running and the next suite member's pre-flight refused to start."""
    from types import SimpleNamespace

    import bench.cleanup
    import bench.client
    import config
    from bench.trials import run_trial

    monkeypatch.setenv("BENCH_ENV_MODE", "live")
    monkeypatch.setattr(config, "case_timeout_s", lambda _baseline=None: 0.05)

    async def placed_an_order_then_hung(case, model):
        trace = bench.client._PARTIAL_TRACE.get()
        call = {"tool": "mcp__bench_staging__create_order_executor", "tool_call_id": "t1", "args": {}}
        trace.turns.append(([call], [{"tool_call_id": "t1", "output": '{"executor_id": "ex1"}'}]))
        await asyncio.sleep(10)

    torn: list = []

    async def fake_teardown(result, model, **kw):
        torn.append([c["tool"] for c in result.tool_calls])
        return SimpleNamespace(resources=[])

    monkeypatch.setattr(bench.client, "run_case", placed_an_order_then_hung)
    monkeypatch.setattr(bench.cleanup, "teardown", fake_teardown)
    case = SimpleNamespace(id="c1", type="tool", risk_level="destructive", expected_tools=[])
    cards: list = []
    asyncio.run(run_trial(case, "m", SimpleNamespace(load=lambda _: None), 1, cards, {}))

    assert len(cards) == 1 and "ceiling" in cards[0].harness_artifact
    assert torn == [["mcp__bench_staging__create_order_executor"]]
