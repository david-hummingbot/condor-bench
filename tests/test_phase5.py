"""Phase 5: NVIDIA NIM provider, L0 contract evals, optional traces, the ladder."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import typer

from bench import nim
from bench.client import BenchmarkResult, TurnResult


# ── NIM ────────────────────────────────────────────────────────────────────────
def test_only_nim_latency_is_unrepresentative():
    assert nim.is_nim_model("nvidia:openai/gpt-oss-20b")
    assert not nim.latency_representative("nvidia:openai/gpt-oss-20b")
    assert nim.latency_representative("openrouter:openai/gpt-4o-mini")
    assert nim.latency_representative("ollama:qwen2.5:14b")


def test_call_recorder_keeps_status_request_id_and_rate_limits():
    recorder = nim.CallRecorder()
    replies = iter([
        httpx.Response(429, json={"detail": "slow down"}, headers={"nvcf-reqid": "r1"}),
        httpx.Response(200, json={"usage": {"prompt_tokens": 10}}, headers={"nvcf-reqid": "r2"}),
    ])
    transport = httpx.MockTransport(lambda request: next(replies))

    async def go():
        async with httpx.AsyncClient(
            transport=transport,
            event_hooks={"request": [recorder.on_request], "response": [recorder.on_response]},
        ) as client:
            await client.post("https://nim/v1/chat/completions", json={})
            await client.post("https://nim/v1/chat/completions", json={})

    asyncio.run(go())
    summary = recorder.summary()
    assert summary["requests"] == 2
    assert summary["statuses"] == {"429": 1, "200": 1}
    assert summary["rate_limit_events"] == 1
    assert summary["request_ids"] == ["r1", "r2"]
    assert summary["calls"][1]["usage"] == {"prompt_tokens": 10}
    assert summary["latency_representative"] is False


def test_nim_models_build_through_the_openai_compatible_path(monkeypatch):
    from condor_compat.acp.pydantic_ai_client import PydanticAIClient, resolve_base_url

    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
    client = PydanticAIClient(model="nvidia:openai/gpt-oss-20b")
    client._build_model()
    assert isinstance(client.provider_log, nim.CallRecorder)
    # One request in flight per process: the semaphore keys on this base URL.
    assert resolve_base_url("nvidia:openai/gpt-oss-20b") == nim.NIM_BASE_URL


def test_nim_without_a_key_fails_loudly(monkeypatch):
    from condor_compat.acp.pydantic_ai_client import PydanticAIClient

    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="NVIDIA_API_KEY"):
        PydanticAIClient(model="nvidia:openai/gpt-oss-20b")._build_model()


def _result(model: str) -> BenchmarkResult:
    return BenchmarkResult(
        case_id="c", model=model,
        turns=[TurnResult(response="BTC is 65000", tool_calls=[{"tool": "get_prices", "args": {}}], latency_s=40.0)],
    )


@pytest.mark.parametrize("model,representative", [
    ("nvidia:openai/gpt-oss-20b", False),
    ("openrouter:openai/gpt-4o-mini", True),
])
def test_nim_rows_are_kept_out_of_latency_gates(monkeypatch, model, representative):
    import bench.scorer as scorer
    from types import SimpleNamespace

    async def fake_a_score(*a, **k):
        return 0.9, "ok"

    monkeypatch.setattr(scorer, "_quality_metric", SimpleNamespace(a_score=fake_a_score))
    card = asyncio.run(scorer.score(_result(model), "price?", ["get_prices"], 10.0))
    assert card.latency_representative is representative
    if representative:
        assert card.latency_score is not None
    else:
        assert card.latency_score is None, "a 40s NIM call must not score 0.25 on latency"
        assert card.time_to_first_valid_action is None


def test_summaries_and_matrix_leave_nim_latency_out(tmp_path, monkeypatch):
    import bench.reporter as reporter
    from bench.matrix import Cell
    from bench.scorer import ScoreCard

    monkeypatch.setattr(reporter, "RESULTS_DIR", tmp_path)
    card = ScoreCard(case_id="c", model="nvidia:x", answer_quality=0.9, answer_reason="",
                     tool_accuracy=1.0, latency_score=None, composite=0.9, latency_s=40.0,
                     baseline_latency_s=10.0, latency_representative=False)
    summary = json.loads((reporter.save_run("nvidia:x", [card], {}, "r") / "summary.json").read_text())
    assert summary["latency_score_avg"] is None
    assert summary["latency_s"] is None
    assert summary["latency_representative"] is False

    cell = Cell()
    cell.add({"case_id": "c", "composite": 0.9, "passed": True, "latency_s": 40.0,
              "latency_representative": False, "usage": {}})
    assert cell.as_dict()["avg_latency_s"] is None


# ── L0 contracts ───────────────────────────────────────────────────────────────
def test_contract_cases_cover_schema_required_and_enum():
    from eval.l0_contracts.contracts import INVALID_ENUM, build_cases

    surface = {"mcp-hummingbot": [
        {"name": "manage_controllers", "inputSchema": {
            "type": "object", "required": ["action"],
            "properties": {"action": {"enum": ["list", "describe"]}}}},
        {"name": "get_prices", "inputSchema": {
            "type": "object", "required": ["connector_name", "trading_pairs"],
            "properties": {"connector_name": {"type": "string"}, "trading_pairs": {"type": "array"}}}},
        {"name": "unmeasured", "inputSchema": {"type": "object"}},
    ]}
    names = [c.name for c in build_cases(surface, only={"manage_controllers", "get_prices"})]
    assert names == [
        "schema:get_prices", "required:get_prices:connector_name",
        "schema:manage_controllers", "required:manage_controllers:action",
        "enum:manage_controllers:action",
    ]
    enum_case = build_cases(surface, only={"manage_controllers"})[-1]
    assert enum_case.inputs.args == {"action": INVALID_ENUM}


def _ctx(kind, outcome, target="action"):
    from types import SimpleNamespace

    from eval.l0_contracts.contracts import ContractInput

    return SimpleNamespace(inputs=ContractInput(kind, "s", "t", {}, target), output=outcome)


def test_contract_evaluators_fail_what_they_should():
    from eval.l0_contracts.contracts import (
        ContractOutcome, RejectedBeforeRunning, SchemaCompiles, StableErrorFormat,
    )

    assert not SchemaCompiles().evaluate(_ctx("schema", ContractOutcome(schema_error="bad type")))
    assert not RejectedBeforeRunning().evaluate(_ctx("required", ContractOutcome(rejected=False)))
    unnamed = ContractOutcome(rejected=True, error_text="validation error", classified_as_error=True)
    assert not StableErrorFormat().evaluate(_ctx("required", unnamed))
    good = ContractOutcome(rejected=True, error_text="1 validation error ... action Field required",
                           classified_as_error=True)
    assert StableErrorFormat().evaluate(_ctx("required", good))


# ── Traces ─────────────────────────────────────────────────────────────────────
def test_traces_are_off_by_default(monkeypatch):
    from bench import traces

    traces.reset_for_tests()
    monkeypatch.delenv("BENCH_TRACES", raising=False)
    monkeypatch.delenv("BENCH_OTLP_ENDPOINT", raising=False)
    with traces.case_span("c", trial=1, model="m", lane="mock") as link:
        assert link == {"trace_id": None, "trace_url": None}
    traces.reset_for_tests()


def test_a_traced_trial_links_its_row_to_the_trace(monkeypatch):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from pydantic_ai import Agent

    from bench import traces

    traces.reset_for_tests()
    exporter = InMemorySpanExporter()
    try:
        assert traces.configure(exporter=exporter)
        with traces.case_span("c1", trial=2, model="m", lane="mock") as link:
            pass
        spans = exporter.get_finished_spans()
        assert [s.name for s in spans] == ["bench.case"]
        assert spans[0].attributes["bench.case_id"] == "c1"
        assert link["trace_id"] == format(spans[0].context.trace_id, "032x")
        assert link["trace_url"].endswith(f"/redirects/traces/{link['trace_id']}")
    finally:
        Agent.instrument_all(False)
        traces.reset_for_tests()


# ── Ladder ─────────────────────────────────────────────────────────────────────
def test_ladder_refuses_unknown_rungs_and_l1_without_a_model():
    import runner

    with pytest.raises(typer.Exit):
        runner.ladder(model=None, upto="l9", trials=1)
    with pytest.raises(typer.Exit):
        runner.ladder(model=None, upto="l1", trials=1)


def test_the_l1_slice_is_mock_servable_single_tool_cases():
    import runner
    from bench.dataset import filter_cases, load_all_cases

    cases = filter_cases(load_all_cases(), layers=["tool"], task_categories=runner.L1_CATEGORIES,
                         splits=["dev", "regression"])
    assert len(cases) >= 10
    assert all(c.type == "tool" for c in cases)
    assert not any(c.task["split"] == "holdout" for c in cases)
