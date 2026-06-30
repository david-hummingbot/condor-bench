"""Claude Sonnet judge wired to DeepEval's BaseLLM interface.

Used by GEval and other deepeval metrics as the evaluation model.
Mirrors the pattern in condor-evals/evals/judge.py.
"""

from __future__ import annotations

import json
import os
from typing import Any

import anthropic
from deepeval.models import DeepEvalBaseLLM

from config import JUDGE_MODEL


class ClaudeJudge(DeepEvalBaseLLM):
    """DeepEval judge backed by Claude Sonnet via the Anthropic API."""

    def __init__(self, model: str | None = None) -> None:
        self.model_name = model or JUDGE_MODEL
        self._client = anthropic.Anthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY")
        )
        self._async_client = anthropic.AsyncAnthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY")
        )

    def load_model(self) -> anthropic.Anthropic:
        return self._client

    def generate(self, prompt: str, schema: type | None = None) -> str | Any:
        if schema is not None:
            return self._generate_structured(prompt, schema)
        msg = self._client.messages.create(
            model=self.model_name,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text

    async def a_generate(self, prompt: str, schema: type | None = None) -> str | Any:
        if schema is not None:
            return await self._a_generate_structured(prompt, schema)
        msg = await self._async_client.messages.create(
            model=self.model_name,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text

    def _generate_structured(self, prompt: str, schema: type) -> Any:
        raw = self.generate(f"{prompt}\n\nRespond with valid JSON only.")
        try:
            data = json.loads(
                raw.strip().removeprefix("```json").removesuffix("```").strip()
            )
            return schema(**data)
        except Exception:
            return schema.model_validate_json(raw)

    async def _a_generate_structured(self, prompt: str, schema: type) -> Any:
        raw = await self.a_generate(f"{prompt}\n\nRespond with valid JSON only.")
        try:
            data = json.loads(
                raw.strip().removeprefix("```json").removesuffix("```").strip()
            )
            return schema(**data)
        except Exception:
            return schema.model_validate_json(raw)

    def get_model_name(self) -> str:
        return f"claude:{self.model_name}"
