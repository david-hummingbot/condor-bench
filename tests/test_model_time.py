"""Model time: the turn's wall clock with the tools' round trips taken out.

The event sequences below are the real streams of the two transports, recorded
with timestamps against condor's MCP servers (see bench.client._TurnClock).
"""

from __future__ import annotations

import asyncio

import pytest

from condor_compat.acp.client import (
    Heartbeat,
    PromptDone,
    TextChunk,
    ThoughtChunk,
    ToolCallEvent,
    ToolCallUpdate,
    UsageEvent,
)


class _Clock:
    now = 0.0

    def monotonic(self) -> float:
        return self.now


def _replay(monkeypatch, timeline, *, streamed: bool):
    """Run _stream_turn over (t, event) pairs on a scripted clock."""
    import bench.client as client_mod

    clock = _Clock()
    monkeypatch.setattr(client_mod, "time", clock)

    class Client:
        async def prompt_stream(self, prompt):
            for t, event in timeline:
                clock.now = t
                yield event

    def run():
        return asyncio.run(client_mod._stream_turn(Client(), "q", {}, streamed=streamed))

    return run()


def _call(i, name, status="pending", **kw):
    return ToolCallEvent(tool_call_id=i, title=name, status=status, kind=kw.pop("kind", "other"), **kw)


# claude-code:haiku over ACP (trimmed to one ToolSearch): thoughts, ToolSearch, then two condor calls in
# one response whose arguments stream in after each is announced.
ACP_STREAM = [
    (2.28, UsageEvent(context_used=32271)),
    (2.28, ThoughtChunk(text="Let me find...")),
    (2.46, ThoughtChunk(text="the tool")),
    (3.72, _call("a", "ToolSearch")),
    (4.02, ToolCallUpdate(tool_call_id="a", title="ToolSearch", input={"q": "x"})),
    (4.17, ToolCallUpdate(tool_call_id="a", title="ToolSearch", input={"q": "x", "n": 5})),
    (4.18, ToolCallUpdate(tool_call_id="a", output="found")),
    (4.18, UsageEvent(context_used=32468)),
    (4.18, ToolCallUpdate(tool_call_id="a", status="completed", output="found")),
    (5.16, UsageEvent(context_used=33059)),
    (8.24, _call("r", "mcp__condor__manage_routines")),
    (8.52, ToolCallUpdate(tool_call_id="r", input={"action": "list"})),
    (8.53, _call("g", "mcp__condor__manage_agents")),
    (8.73, ToolCallUpdate(tool_call_id="g", input={"action": "list"})),
    (8.73, UsageEvent(context_used=45682)),
    (9.04, ToolCallUpdate(tool_call_id="r", output="{routines}")),
    (9.04, ToolCallUpdate(tool_call_id="r", status="completed", output="{routines}")),
    (9.06, ToolCallUpdate(tool_call_id="g", output="{agents}")),
    (9.06, ToolCallUpdate(tool_call_id="g", status="completed", output="{agents}")),
    (9.91, UsageEvent(context_used=48346)),
    (15.46, TextChunk(text="There are 6 routines")),
    (15.47, UsageEvent(context_used=48908)),
    (16.53, UsageEvent(input_tokens=38, output_tokens=1066)),
    (16.53, PromptDone(stop_reason="end_turn")),
]

# deepseek over pydantic-ai: no streaming. A response's parts arrive when it
# completes; the tools' results arrive once they ran, as the next request starts.
PAI_STREAM = [
    (0.00, Heartbeat(elapsed_seconds=0.0)),
    (5.58, TextChunk(text="Listing both.")),
    (5.58, _call("r", "manage_routines", status="in_progress", kind="mcp", input={"action": "list"})),
    (5.58, ToolCallUpdate(tool_call_id="r", status="completed")),
    (5.58, _call("g", "manage_agents", status="in_progress", kind="mcp", input={"action": "list"})),
    (5.58, ToolCallUpdate(tool_call_id="g", status="completed")),
    (6.11, Heartbeat(elapsed_seconds=6.11)),
    (6.11, ToolCallUpdate(tool_call_id="r", status="completed", output="{routines}")),
    (6.11, ToolCallUpdate(tool_call_id="g", status="completed", output="{agents}")),
    (11.18, TextChunk(text="There are 6 routines and 9 agents.")),
    (11.18, UsageEvent(input_tokens=83925, output_tokens=376)),
    (11.18, PromptDone(stop_reason="end_turn")),
]


def test_acp_tools_are_billed_from_their_last_argument_not_their_announcement(monkeypatch):
    turn = _replay(monkeypatch, ACP_STREAM, streamed=True)
    tm = turn.timing
    # Tools: 4.17→4.18 (ToolSearch), and 8.73→9.06 for the two condor calls that
    # ran together after the model finished writing both. Counting from each
    # announcement would have billed the tools ~0.8s of the model's typing.
    assert tm["tool_s"] == pytest.approx(0.01 + 0.33, abs=0.005)
    assert tm["other_s"] == pytest.approx(16.53 - 15.46, abs=0.005)
    assert tm["model_s"] + tm["tool_s"] + tm["other_s"] == pytest.approx(turn.latency_s)
    # Three requests: the opening one (thoughts, ToolSearch), the one after its
    # result (both condor calls, in one response), and the answer.
    assert tm["requests"] == 3
    assert tm["ttft_s"][0] == pytest.approx(2.28)
    assert tm["streamed"] is True


def test_pydantic_ai_tools_are_the_gap_between_the_response_and_the_results(monkeypatch):
    turn = _replay(monkeypatch, PAI_STREAM, streamed=False)
    tm = turn.timing
    assert tm["model_s"] == pytest.approx(5.58 + (11.18 - 6.11))
    assert tm["tool_s"] == pytest.approx(6.11 - 5.58), "parallel calls count once"
    assert tm["requests"] == 2
    assert tm["streamed"] is False


def test_each_tool_result_records_when_it_arrived(monkeypatch):
    turn = _replay(monkeypatch, PAI_STREAM, streamed=False)
    assert [r["t_s"] for r in turn.tool_responses] == [6.11, 6.11]


def test_later_turns_offset_their_results_by_the_turns_before():
    from bench.client import BenchmarkResult, TurnResult

    def turn(latency, *offsets):
        return TurnResult(
            response="", tool_calls=[], latency_s=latency,
            tool_responses=[{"tool": "x", "output": "", "t_s": o} for o in offsets],
        )

    result = BenchmarkResult(case_id="c", model="m", turns=[turn(10.0, 2.0), turn(5.0, 1.0)])
    assert result.response_offsets_s() == [2.0, 11.0]


# ── Scoring ────────────────────────────────────────────────────────────────────
def _timed_result(model_s, tool_s, *, streamed=False, ttft=(1.0,), output_tokens=500):
    from bench.client import BenchmarkResult, TurnResult

    return BenchmarkResult(
        case_id="c1",
        model="openrouter:x/y",
        turns=[TurnResult(
            response="done", tool_calls=[], latency_s=model_s + tool_s,
            timing={"model_s": model_s, "tool_s": tool_s, "other_s": 0.0,
                    "requests": len(ttft), "ttft_s": list(ttft), "streamed": streamed},
        )],
        usage={"output_tokens": output_tokens},
    )


def _score(result, baseline_wall, baseline_model):
    from unittest.mock import AsyncMock, patch

    from bench.scorer import score

    with patch("bench.scorer._quality_metric.a_score", new_callable=AsyncMock) as q:
        q.return_value = (1.0, "ok")
        return asyncio.run(score(
            result, "q", None, baseline_wall, baseline_model_s=baseline_model,
        ))


def test_latency_score_compares_model_time_when_the_baseline_has_it():
    # Same model speed as the baseline, but this run's tools were 20s slower.
    card = _score(_timed_result(10.0, 25.0), baseline_wall=15.0, baseline_model=10.0)
    assert card.latency_basis == "model"
    assert card.latency_score == 1.0, "slow tools are not a slow model"
    assert card.model_time_s == 10.0 and card.tool_time_s == 25.0
    assert card.output_tps == 50.0


def test_latency_score_falls_back_to_wall_against_an_untimed_baseline():
    card = _score(_timed_result(10.0, 20.0), baseline_wall=15.0, baseline_model=None)
    assert card.latency_basis == "wall"
    assert card.latency_score == pytest.approx(0.5)


def test_ttft_is_only_reported_for_a_streaming_transport():
    assert _score(_timed_result(10.0, 1.0, streamed=False), 11.0, 10.0).ttft_s is None
    card = _score(_timed_result(10.0, 1.0, streamed=True, ttft=(2.0, 0.5, 1.0)), 11.0, 10.0)
    assert card.ttft_s == 1.0, "median over the trial's requests"


# ── Aggregates ─────────────────────────────────────────────────────────────────
def _row(case_id, model_s, passed, **kw):
    return {
        "case_id": case_id, "composite": 0.9 if passed else 0.1, "passed": passed,
        "latency_s": model_s + 5.0, "model_time_s": model_s, "latency_representative": True,
        **kw,
    }


def test_model_time_per_success_charges_failures_to_the_successes():
    from bench.matrix import Cell as _Cell

    cell = _Cell()
    for row in (_row("a", 10.0, True), _row("b", 30.0, False), _row("c", 20.0, True)):
        cell.add(row)
    out = cell.as_dict()
    assert out["model_time_per_success"] == 30.0  # 60s of model time, two successes
    assert out["median_model_time_s"] == 20.0


def test_untimed_and_unrepresentative_rows_stay_out_of_model_time():
    from bench.matrix import Cell as _Cell

    cell = _Cell()
    cell.add(_row("a", 10.0, True))
    cell.add({**_row("b", 99.0, True), "model_time_s": None})
    cell.add({**_row("c", 99.0, True), "latency_representative": False})
    assert cell.as_dict()["model_time_per_success"] == 10.0
