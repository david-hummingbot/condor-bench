"""Claude judge — calls the Anthropic API directly, no DeepEval dependency.

Judge tokens are accounted for separately from the model under test. Mixing them
would make a cheap local model look expensive (every case it runs is judged by
Sonnet), and the point of tracking cost at all is to compare the *candidates*.
"""
from __future__ import annotations

import os
import threading

import anthropic

from config import JUDGE_MODEL


class JudgeUsage:
    """Thread-safe running total of judge tokens and cost.

    A judge call happens once per case, from ``AnswerQualityMetric``, which the
    scorer awaits — so per-case attribution means reading the delta around that
    call rather than threading a usage object through every metric signature.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0

    def add(self, input_tokens: int, output_tokens: int) -> None:
        with self._lock:
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            self.calls += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "calls": self.calls,
            }

    def delta_since(self, before: dict[str, int], model: str | None = None) -> dict:
        """Usage accrued since ``before``, priced. Empty dict when nothing was spent."""
        now = self.snapshot()
        input_tokens = now["input_tokens"] - before.get("input_tokens", 0)
        output_tokens = now["output_tokens"] - before.get("output_tokens", 0)
        if input_tokens <= 0 and output_tokens <= 0:
            return {}
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "calls": now["calls"] - before.get("calls", 0),
            "cost_usd": judge_cost_usd(
                model or JUDGE_MODEL,
                {"input_tokens": input_tokens, "output_tokens": output_tokens},
            ),
        }


# Module-level: every ClaudeJudge instance reports into the same tally, so a
# scorer that constructs its own metric objects still gets complete numbers.
JUDGE_USAGE = JudgeUsage()


def judge_cost_usd(model: str, usage: dict[str, int]) -> float | None:
    """Price judge tokens with the same offline dataset used for model cost.

    The judge model is a bare Anthropic id (``claude-sonnet-4-6``), not a
    prefixed agent key, so it is qualified here before pricing.
    """
    from condor_compat.acp.pydantic_ai_client import estimate_cost_usd

    key = model if ":" in model else f"anthropic:{model}"
    return estimate_cost_usd(key, usage)


class ClaudeJudge:
    def __init__(self, model: str | None = None) -> None:
        self.model_name = model or JUDGE_MODEL
        self._client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        self._async_client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    def generate(self, prompt: str) -> str:
        msg = self._client.messages.create(
            model=self.model_name,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        _record(msg)
        return msg.content[0].text

    async def a_generate(self, prompt: str) -> str:
        msg = await self._async_client.messages.create(
            model=self.model_name,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        _record(msg)
        return msg.content[0].text


def _record(msg: object) -> None:
    usage = getattr(msg, "usage", None)
    if usage is None:
        return
    JUDGE_USAGE.add(
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
    )
