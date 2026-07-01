"""Claude judge — calls the Anthropic API directly, no DeepEval dependency."""
from __future__ import annotations

import json
import os

import anthropic

from config import JUDGE_MODEL


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
        return msg.content[0].text

    async def a_generate(self, prompt: str) -> str:
        msg = await self._async_client.messages.create(
            model=self.model_name,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text
