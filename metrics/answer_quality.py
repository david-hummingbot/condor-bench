"""Answer Quality metric — reference-free judge evaluation.

Calls the Claude judge directly (no DeepEval GEval) to avoid internal
OpenAI routing that GEval performs regardless of the custom model.

Score: 0.0 (wrong / unhelpful) → 1.0 (excellent).
"""
from __future__ import annotations

import json

from pydantic import BaseModel

from metrics.judge import ClaudeJudge

_CRITERIA = """\
You are evaluating a response from Condor, an automated crypto trading assistant \
that helps users manage grid trading, DCA, CLMM liquidity provision, and \
trend-following strategies via AI agents backed by pydantic-ai and MCP tools.

Score the ACTUAL RESPONSE against these four criteria:

1. Accuracy — are Condor-specific concepts correct? \
   (grid executors, ticks, CLMM, DCA, drawdown limits, pydantic-ai models, MCP tools, etc.)
2. Completeness — does it fully address what the user asked?
3. Safety — where relevant, are risk management and capital preservation principles respected?
4. Actionability — is the advice coherent and directly useful to a Condor user?

Score 1.0 for an excellent answer that is accurate, complete, and actionable. \
Score 0.0 for a response that refuses to answer, gives incorrect trading information, \
or fails to meaningfully address the question. \
DO NOT reward hedging or vagueness — a clear, correct, detailed answer is better \
than a cautious non-answer."""


class _ScoreResult(BaseModel):
    score: float
    reason: str


def _parse(raw: str) -> tuple[float, str]:
    text = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(text)
    score = max(0.0, min(1.0, float(data["score"])))
    return score, str(data.get("reason", ""))


def _build_prompt(input_text: str, actual_output: str) -> str:
    return (
        f"{_CRITERIA}\n\n"
        f"USER INPUT:\n{input_text[:1500]}\n\n"
        f"ACTUAL RESPONSE:\n{actual_output[:3000]}\n\n"
        "Rate this response. Reply with JSON only, no markdown:\n"
        '{"score": <float 0.0–1.0>, "reason": "<one sentence>"}'
    )


class AnswerQualityMetric:
    name = "Answer Quality"

    def __init__(self, threshold: float = 0.6) -> None:
        self.threshold = threshold
        self._judge = ClaudeJudge()

    async def a_score(self, input_text: str, actual_output: str) -> tuple[float, str]:
        if not actual_output.strip():
            return 0.0, "No response produced."
        try:
            raw = await self._judge.a_generate(_build_prompt(input_text, actual_output))
            return _parse(raw)
        except Exception as exc:
            return 0.0, f"Judge error: {exc}"

    def score(self, input_text: str, actual_output: str) -> tuple[float, str]:
        if not actual_output.strip():
            return 0.0, "No response produced."
        try:
            raw = self._judge.generate(_build_prompt(input_text, actual_output))
            return _parse(raw)
        except Exception as exc:
            return 0.0, f"Judge error: {exc}"

    def passes(self, score: float) -> bool:
        return score >= self.threshold
