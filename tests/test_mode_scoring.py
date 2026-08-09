"""Mode-aware composites, weight redistribution, and harness-artifact detection.

Two invariants make live and mock composites comparable at all:

* every mode's applied weights sum to 1.0, so a case with less ground truth is
  not silently capped below 1.0
* adding a metric a case can't be scored on never lowers that case's ceiling
"""

from __future__ import annotations

import pytest

from bench.client import BenchmarkResult, TurnResult
from bench.scorer import _composite, _detect_harness_artifact
from config import SCORE_WEIGHTS_LIVE, SCORE_WEIGHTS_MOCK, score_weights


@pytest.mark.parametrize("weights", [SCORE_WEIGHTS_MOCK, SCORE_WEIGHTS_LIVE])
def test_weight_profiles_sum_to_one(weights):
    assert sum(weights.values()) == pytest.approx(1.0)


def test_mock_profile_gives_live_only_metrics_no_weight():
    """Canned mock payloads are valid by construction; scoring them adds noise."""
    assert SCORE_WEIGHTS_MOCK["live_validity"] == 0.0
    assert SCORE_WEIGHTS_MOCK["tool_params"] == 0.0


def test_live_profile_weights_params_and_validity():
    assert SCORE_WEIGHTS_LIVE["tool_params"] > 0
    assert SCORE_WEIGHTS_LIVE["live_validity"] > 0


def test_score_weights_selects_by_mode():
    assert score_weights("live") is SCORE_WEIGHTS_LIVE
    assert score_weights("mock") is SCORE_WEIGHTS_MOCK
    assert score_weights("nonsense") is SCORE_WEIGHTS_MOCK


def test_all_components_perfect_scores_one():
    for weights in (SCORE_WEIGHTS_MOCK, SCORE_WEIGHTS_LIVE):
        composite = _composite(
            {
                "answer_quality": 1.0,
                "tool_accuracy": 1.0,
                "tool_params": 1.0,
                "live_validity": 1.0,
                "latency_score": 1.0,
            },
            weights,
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
        SCORE_WEIGHTS_LIVE,
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
        SCORE_WEIGHTS_LIVE,
    )
    unscorable = _composite(
        {
            "answer_quality": 0.8,
            "tool_accuracy": 0.8,
            "tool_params": None,
            "live_validity": None,
            "latency_score": 0.8,
        },
        SCORE_WEIGHTS_LIVE,
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
        SCORE_WEIGHTS_LIVE,
    )
    assert failed == pytest.approx(1.0 - SCORE_WEIGHTS_LIVE["tool_accuracy"])


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
        _result({"assistant_prompt": "fallback:vendored (no condor checkout)"}), "mock"
    )
    assert reason and "assistant prompt" in reason


def test_real_agent_prompt_is_not_an_artifact():
    assert (
        _detect_harness_artifact(
            _result({"assistant_prompt": "condor:agents/market_making_expert/AGENT.md"}),
            "mock",
        )
        is None
    )


def test_live_run_without_a_resolved_url_is_an_artifact():
    reason = _detect_harness_artifact(_result({"api_url": None}), "live")
    assert reason and "API URL" in reason


def test_playwright_autodiscovery_is_an_artifact():
    """Extra tools shift the small-model tool cut, so the run isn't comparable."""
    reason = _detect_harness_artifact(
        _result({"api_url": "http://staging:8000", "autodiscovery_extras": ["playwright"]}),
        "live",
    )
    assert reason and "playwright" in reason


def test_clean_live_wiring_is_not_an_artifact():
    assert (
        _detect_harness_artifact(
            _result(
                {
                    "api_url": "http://staging:8000",
                    "autodiscovery_extras": [],
                    "assistant_prompt": None,
                }
            ),
            "live",
        )
        is None
    )


def test_mock_runs_are_not_judged_on_live_wiring():
    assert _detect_harness_artifact(_result({}), "mock") is None


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
