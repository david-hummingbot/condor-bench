"""Answer Quality metric — reference-free judge evaluation.

The judge scores each response on its own merits against Condor domain criteria.
No baseline comparison: a better-than-baseline answer always scores higher.

Score: 0.0 (wrong / unhelpful) → 1.0 (excellent).
"""
from __future__ import annotations

from deepeval.metrics import GEval
from deepeval.test_case import LLMTestCase, LLMTestCaseParams

from metrics.judge import ClaudeJudge

_JUDGE = ClaudeJudge()

_CRITERIA = """You are evaluating a response from Condor, an automated crypto trading assistant that helps users manage grid trading, DCA, CLMM liquidity provision, and trend-following strategies via AI agents backed by pydantic-ai and MCP tools.

Score the ACTUAL RESPONSE against these four criteria:

1. Accuracy — are Condor-specific concepts correct?    (grid executors, ticks, CLMM, DCA, drawdown limits, pydantic-ai models, MCP tools, etc.)
2. Completeness — does it fully address what the user asked?
3. Safety — where relevant, are risk management and capital preservation principles respected?
4. Actionability — is the advice coherent and directly useful to a Condor user?

Score 1.0 for an excellent answer that is accurate, complete, and actionable. Score 0.0 for a response that refuses to answer, gives incorrect trading information, or fails to meaningfully address the question. DO NOT reward hedging or vagueness — a clear, correct, detailed answer is better than a cautious non-answer."""


def _make_metric(threshold: float = 0.6) -> GEval:
    return GEval(
        name="Answer Quality",
        criteria=_CRITERIA,
        evaluation_params=[
            LLMTestCaseParams.INPUT,
            LLMTestCaseParams.ACTUAL_OUTPUT,
        ],
        model=_JUDGE,
        threshold=threshold,
    )


class AnswerQualityMetric:
    name = "Answer Quality"

    def __init__(self, threshold: float = 0.6) -> None:
        self.threshold = threshold
        self._metric = _make_metric(threshold)

    async def a_score(self, input_text: str, actual_output: str) -> tuple[float, str]:
        if not actual_output.strip():
            return 0.0, "No response produced."
        test_case = LLMTestCase(input=input_text[:2000], actual_output=actual_output)
        await self._metric.a_measure(test_case)
        return float(self._metric.score or 0.0), str(self._metric.reason or "").strip()

    def score(self, input_text: str, actual_output: str) -> tuple[float, str]:
        if not actual_output.strip():
            return 0.0, "No response produced."
        test_case = LLMTestCase(input=input_text[:2000], actual_output=actual_output)
        self._metric.measure(test_case)
        return float(self._metric.score or 0.0), str(self._metric.reason or "").strip()

    def passes(self, score: float) -> bool:
        return score >= self.threshold
