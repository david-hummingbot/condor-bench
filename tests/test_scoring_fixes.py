"""Unit tests for scoring / tool accuracy fixes."""
from __future__ import annotations

from bench.client import BenchmarkResult, TurnResult, _asks_confirmation
from bench.scorer import normalize_expected_tools
from metrics.answer_quality import is_infra_failure
from metrics.tool_accuracy import ToolAccuracyMetric, normalize_tool_name


def test_normalize_tool_name_strips_mcp_prefix():
    assert normalize_tool_name("mcp__condor__manage_routines") == "manage_routines"
    assert normalize_tool_name("get_market_data") == "get_market_data"


def test_empty_expected_tools_normalize_to_none():
    assert normalize_expected_tools([]) is None
    assert normalize_expected_tools(None) is None
    assert normalize_expected_tools(["manage_executors"]) == ["manage_executors"]


def test_f1_identical():
    m = ToolAccuracyMetric()
    assert m.score(["a", "b"], ["a", "b"]) == 1.0


def test_f1_both_empty():
    m = ToolAccuracyMetric()
    assert m.score([], []) == 1.0


def test_forbidden_tools_zero_score():
    m = ToolAccuracyMetric()
    assert m.score(
        ["get_market_data", "manage_executors"],
        ["get_market_data"],
        forbidden_tools=["manage_executors"],
    ) == 0.0


def test_forbidden_ok_when_not_called():
    m = ToolAccuracyMetric()
    score = m.score(
        ["get_market_data"],
        ["get_market_data"],
        forbidden_tools=["manage_executors"],
    )
    assert score == 1.0


def test_prefix_normalized_in_f1():
    m = ToolAccuracyMetric()
    score = m.score(
        ["mcp__mcp-hummingbot__get_market_data"],
        ["get_market_data"],
    )
    assert score == 1.0


def test_infra_failure_detection():
    assert is_infra_failure("(error: The next request would exceed the request_limit of 50)")
    assert is_infra_failure("(error: Model token limit (provider default) exceeded before any response was generated)")
    assert not is_infra_failure("Restarting after max drawdown is risky; tighten limits first.")


def test_asks_confirmation():
    assert _asks_confirmation("Shall I deploy this grid executor? (yes/no)")
    assert _asks_confirmation("Reply with CONFIRM DCA to proceed.")
    assert not _asks_confirmation("Grid executor grid-btc-001 created successfully.")


def test_transcript_for_judge_includes_tools():
    result = BenchmarkResult(
        case_id="x",
        model="m",
        turns=[
            TurnResult("first", [{"tool": "get_market_data", "args": {}}], 1.0),
            TurnResult("second", [{"tool": "manage_executors", "args": {}}], 1.0),
        ],
    )
    text = result.transcript_for_judge()
    assert "Turn 1" in text and "Turn 2" in text
    assert "get_market_data" in text and "manage_executors" in text
