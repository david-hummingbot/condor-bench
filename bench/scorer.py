"""Score a benchmark result.

Composite = 50% answer quality (reference-free judge)
          + 30% tool accuracy (vs dataset expected_tools)
          + 20% latency score (vs baseline latency)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config import LATENCY_FLOOR, SCORE_WEIGHTS
from bench.client import BenchmarkResult
from metrics.answer_quality import AnswerQualityMetric
from metrics.tool_accuracy import ToolAccuracyMetric
from metrics.latency import LatencyMetric

_quality_metric = AnswerQualityMetric()
_tool_metric = ToolAccuracyMetric()
_latency_metric = LatencyMetric()


@dataclass
class ScoreCard:
    case_id: str
    model: str
    answer_quality: float
    answer_reason: str
    tool_accuracy: float
    latency_score: float
    composite: float
    latency_s: float
    baseline_latency_s: float
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "model": self.model,
            "answer_quality": round(self.answer_quality, 4),
            "answer_reason": self.answer_reason,
            "tool_accuracy": round(self.tool_accuracy, 4),
            "latency_score": round(self.latency_score, 4),
            "composite": round(self.composite, 4),
            "latency_s": round(self.latency_s, 2),
            "baseline_latency_s": round(self.baseline_latency_s, 2),
            "error": self.error,
        }


async def score(
    result: BenchmarkResult,
    input_text: str,
    expected_tools: list[str],
    baseline_latency_s: float,
) -> ScoreCard:
    """Score a result against dataset ground truth (no baseline response needed)."""
    answer_quality, answer_reason = await _quality_metric.a_score(input_text, result.response)

    tool_accuracy = _tool_metric.score(
        actual_tools=result.tool_names(),
        expected_tools=expected_tools,
    )

    latency_score = _latency_metric.score(
        test_latency=result.latency_s,
        baseline_latency=baseline_latency_s,
    )

    weights = SCORE_WEIGHTS
    composite = (
        weights["answer_quality"] * answer_quality
        + weights["tool_accuracy"] * tool_accuracy
        + weights["latency_score"] * latency_score
    )

    return ScoreCard(
        case_id=result.case_id,
        model=result.model,
        answer_quality=answer_quality,
        answer_reason=answer_reason,
        tool_accuracy=tool_accuracy,
        latency_score=latency_score,
        composite=composite,
        latency_s=result.latency_s,
        baseline_latency_s=baseline_latency_s,
        error=result.error,
    )
