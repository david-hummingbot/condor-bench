"""Composite weighting, weight redistribution, and harness-artifact detection.

Two invariants keep composites comparable across cases:

* the applied weights sum to 1.0, so a case with less ground truth is not
  silently capped below 1.0
* adding a metric a case can't be scored on never lowers that case's ceiling
"""

from __future__ import annotations

import pytest

from bench.client import BenchmarkResult, TurnResult
from bench.scorer import _composite, _detect_harness_artifact
from config import SCORE_WEIGHTS


def test_weights_sum_to_one():
    assert sum(SCORE_WEIGHTS.values()) == pytest.approx(1.0)


def test_params_and_validity_carry_weight():
    """Real API responses make both meaningful, so neither is a free pass."""
    assert SCORE_WEIGHTS["tool_params"] > 0
    assert SCORE_WEIGHTS["live_validity"] > 0


def test_all_components_perfect_scores_one():
    composite = _composite(
        {
            "answer_quality": 1.0,
            "tool_accuracy": 1.0,
            "tool_params": 1.0,
            "live_validity": 1.0,
            "latency_score": 1.0,
        }
    )
    assert composite == pytest.approx(1.0)


def test_unscorable_component_weight_goes_to_quality():
    """An advisory case with no expected_tools must still be able to reach 1.0."""
    composite = _composite(
        {
            "answer_quality": 1.0,
            "tool_accuracy": None,
            "tool_params": None,
            "live_validity": None,
            "latency_score": 1.0,
        },
    )
    assert composite == pytest.approx(1.0)


def test_unscorable_component_does_not_lower_the_ceiling():
    """Adding a metric a case can't be scored on must not cap it below a scored one."""
    scored = _composite(
        {
            "answer_quality": 0.8,
            "tool_accuracy": 0.8,
            "tool_params": 0.8,
            "live_validity": 0.8,
            "latency_score": 0.8,
        },
    )
    unscorable = _composite(
        {
            "answer_quality": 0.8,
            "tool_accuracy": 0.8,
            "tool_params": None,
            "live_validity": None,
            "latency_score": 0.8,
        },
    )
    assert scored == pytest.approx(unscorable)


def test_zero_scored_component_still_costs_its_weight():
    """Redistribution applies to *unscorable*, not to *failed*."""
    failed = _composite(
        {
            "answer_quality": 1.0,
            "tool_accuracy": 0.0,
            "tool_params": 1.0,
            "live_validity": 1.0,
            "latency_score": 1.0,
        },
    )
    assert failed == pytest.approx(1.0 - SCORE_WEIGHTS["tool_accuracy"])


# ── Harness artifacts ──────────────────────────────────────────────────────────
def _result(wiring: dict) -> BenchmarkResult:
    return BenchmarkResult(
        case_id="x",
        model="m",
        turns=[TurnResult("hi", [], 1.0)],
        wiring=wiring,
    )


def test_prompt_fallback_is_a_harness_artifact():
    """A Layer 3 case graded against the generic prompt tests nothing about the agent."""
    reason = _detect_harness_artifact(
        _result({"assistant_prompt": "fallback:vendored (no condor checkout)"})
    )
    assert reason and "assistant prompt" in reason


def test_real_agent_prompt_is_not_an_artifact():
    assert (
        _detect_harness_artifact(
            _result(
                {
                    "api_url": "http://staging:8000",
                    "assistant_prompt": "condor:agents/market_making_expert/AGENT.md",
                }
            )
        )
        is None
    )


def test_run_without_a_resolved_url_is_an_artifact():
    reason = _detect_harness_artifact(_result({"api_url": None}))
    assert reason and "API URL" in reason


def test_playwright_autodiscovery_is_a_note_not_an_artifact():
    """A caveat, not an exclusion — the row is still evidence about the model.

    This was an artifact on the theory that extra tools shift the small-model
    tool cut. But `_TOOL_LIMITS` lives in `condor_compat.acp.pydantic_ai_client`
    and is never applied on the ACP path, which is the only path that gets
    autodiscovery extras — so the mechanism cannot occur on the affected runs,
    while the exclusion applied to *every* row of them. A full 80-case
    claude-code run reported `cases_scored: 0`, `composite_avg: 0.0` and an empty
    matrix: no routing recommendation could be produced at all.

    The real risk it was reaching for — an expected tool pushed out of view — has
    its own precise rule below, per case, on `offered_tools`.
    """
    from bench.scorer import _detect_harness_note

    wiring = {"api_url": "http://staging:8000", "autodiscovery_extras": ["playwright"]}
    assert _detect_harness_artifact(_result(wiring)) is None
    note = _detect_harness_note(_result(wiring))
    assert note and "playwright" in note


def test_an_expected_tool_never_offered_is_still_an_artifact():
    """The genuine harm keeps its exclusion, per case rather than per transport."""
    reason = _detect_harness_artifact(
        _result(
            {
                "api_url": "http://staging:8000",
                "autodiscovery_extras": ["playwright"],
                "offered_tools": ["get_market_data"],
            }
        ),
        ["manage_executors"],
    )
    assert reason and "manage_executors" in reason


def test_a_bench_probe_prompt_fallback_is_not_an_artifact():
    """`bench_journal_probe` is a journal fixture, not an assistant.

    It has no AGENT.md by design, so the generic Condor prompt is the correct
    prompt for it. Flagging the fallback excluded all four journal cases from
    every run for lacking instructions they were never meant to have.
    """
    assert (
        _detect_harness_artifact(
            _result(
                {
                    "api_url": "http://staging:8000",
                    "agent_slug": "bench_journal_probe",
                    "assistant_prompt": "fallback:vendored (no prompt found)",
                }
            )
        )
        is None
    )


def test_a_real_agent_prompt_fallback_is_still_an_artifact():
    """A Layer 3 case on the generic prompt is not a test of that assistant."""
    reason = _detect_harness_artifact(
        _result(
            {
                "api_url": "http://staging:8000",
                "agent_slug": "market_making_expert",
                "assistant_prompt": "fallback:vendored (no prompt found)",
            }
        )
    )
    assert reason and "fell back" in reason


def test_clean_wiring_is_not_an_artifact():
    assert (
        _detect_harness_artifact(
            _result(
                {
                    "api_url": "http://staging:8000",
                    "autodiscovery_extras": [],
                    "assistant_prompt": None,
                }
            )
        )
        is None
    )


# ── Judge response parsing ─────────────────────────────────────────────────────
class _Block:
    def __init__(self, type_: str, text: str | None = None) -> None:
        self.type = type_
        if text is not None:
            self.text = text


class _Msg:
    def __init__(self, *blocks) -> None:
        self.content = list(blocks)


def test_judge_text_extraction_skips_thinking_blocks():
    """A thinking-first response must not zero every quality score.

    `msg.content[0].text` raised on models that return extended thinking, and
    AnswerQualityMetric swallows judge errors as 0.0 — so the suite scored zero
    quality across the board while looking like it had run correctly.
    """
    from metrics.judge import _text_of

    msg = _Msg(_Block("thinking"), _Block("text", '{"score": 0.9, "reason": "ok"}'))
    assert _text_of(msg) == '{"score": 0.9, "reason": "ok"}'


def test_judge_text_extraction_joins_multiple_text_blocks():
    from metrics.judge import _text_of

    assert _text_of(_Msg(_Block("text", "a"), _Block("text", "b"))) == "ab"


def test_judge_with_no_text_block_raises_rather_than_returning_empty():
    """An empty string would fail later as a confusing JSON parse error."""
    from metrics.judge import _text_of

    with pytest.raises(ValueError, match="no text block"):
        _text_of(_Msg(_Block("thinking")))
