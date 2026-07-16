"""Score a benchmark result.

Composite = 50% answer quality (reference-free judge)
          + 30% tool accuracy (vs dataset expected_tools)
          + 20% latency score (vs baseline latency)

Empty expected_tools ([]) means no required tools — tool accuracy is skipped
and its weight is redistributed to answer quality (same as None).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from config import SCORE_WEIGHTS
from bench.client import BenchmarkResult
from metrics.answer_quality import AnswerQualityMetric, is_infra_failure
from metrics.tool_accuracy import ToolAccuracyMetric
from metrics.latency import LatencyMetric

_quality_metric = AnswerQualityMetric()
_tool_metric = ToolAccuracyMetric()
_latency_metric = LatencyMetric()


def normalize_expected_tools(expected_tools: list[str] | None) -> list[str] | None:
    """Empty list → None (advisory: no tool ground truth)."""
    if expected_tools is None:
        return None
    if len(expected_tools) == 0:
        return None
    return expected_tools


@dataclass
class ScoreCard:
    case_id: str
    model: str
    answer_quality: float
    answer_reason: str
    tool_accuracy: float | None  # None when no required expected_tools
    latency_score: float
    composite: float
    latency_s: float
    baseline_latency_s: float
    error: str | None = None
    category: str = ""
    tool_calls: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "model": self.model,
            "category": self.category,
            "answer_quality": round(self.answer_quality, 4),
            "answer_reason": self.answer_reason,
            "tool_accuracy": round(self.tool_accuracy, 4) if self.tool_accuracy is not None else None,
            "latency_score": round(self.latency_score, 4),
            "composite": round(self.composite, 4),
            "latency_s": round(self.latency_s, 2),
            "baseline_latency_s": round(self.baseline_latency_s, 2),
            "error": self.error,
            "tool_calls": self.tool_calls,
        }


async def score(
    result: BenchmarkResult,
    input_text: str,
    expected_tools: list[str] | None,
    baseline_latency_s: float,
    *,
    expected_no_calls: list[str] | None = None,
) -> ScoreCard:
    """Score a result against dataset ground truth (no baseline response needed).

    When expected_tools is None or [] (no required tools), tool accuracy is
    skipped and its weight is redistributed to answer quality.

    expected_no_calls: tools that must NOT appear; any hit → tool_accuracy 0.
    """
    tool_names = result.tool_names()
    judge_input = input_text
    judge_output = result.transcript_for_judge()

    # Provider/infra failures — mark error so summary averages exclude them
    infra_blob = result.response or (result.error or "")
    if is_infra_failure(infra_blob):
        raw = infra_blob.strip() or "empty infra failure"
        return ScoreCard(
            case_id=result.case_id,
            model=result.model,
            answer_quality=0.0,
            answer_reason=f"Infrastructure error: {raw[:160]}",
            tool_accuracy=None,
            latency_score=0.0,
            composite=0.0,
            latency_s=result.latency_s,
            baseline_latency_s=baseline_latency_s,
            error=f"infra: {raw[:200]}",
            tool_calls=tool_names,
        )

    answer_quality, answer_reason = await _quality_metric.a_score(judge_input, judge_output)

    required = normalize_expected_tools(expected_tools)
    forbidden = list(expected_no_calls or [])
    tool_accuracy: float | None = None
    if required is not None:
        tool_accuracy = _tool_metric.score(
            actual_tools=tool_names,
            expected_tools=required,
            forbidden_tools=forbidden or None,
        )
    elif forbidden:
        # Only a must-not-call list: 1.0 if clean, else 0.0
        tool_accuracy = (
            0.0
            if ToolAccuracyMetric.violated_forbidden(tool_names, forbidden)
            else 1.0
        )

    latency_score = _latency_metric.score(
        test_latency=result.latency_s,
        baseline_latency=baseline_latency_s,
    )

    weights = SCORE_WEIGHTS
    if tool_accuracy is not None:
        composite = (
            weights["answer_quality"] * answer_quality
            + weights["tool_accuracy"] * tool_accuracy
            + weights["latency_score"] * latency_score
        )
    else:
        q_weight = weights["answer_quality"] + weights["tool_accuracy"]
        composite = q_weight * answer_quality + weights["latency_score"] * latency_score

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
        tool_calls=tool_names,
    )
