"""Claude judge — Anthropic API or Claude Code ACP, no DeepEval dependency.

Judge tokens are accounted for separately from the model under test. Mixing them
would make a cheap local model look expensive (every case it runs is judged by
Sonnet), and the point of tracking cost at all is to compare the *candidates*.
"""
from __future__ import annotations

import asyncio
import os
import re
import threading
from pathlib import Path
from typing import Any

import anthropic

from config import (
    JUDGE_BACKEND_ACP,
    ROOT,
    judge_backend,
    judge_model,
)

# Output cap for a judge call. Generous on purpose — see the note at the call
# sites. It is a ceiling, not a spend: judge usage is metered separately and never
# enters a model's score.
JUDGE_MAX_TOKENS = 2048

# ACP Claude Code is an agent: without this it will try to Read/Search instead of
# returning the JSON verdict the scorer parses.
_ACP_JUDGE_PREFIX = (
    "You are a scoring judge. Reply with a single JSON object only. "
    "Do not call tools, read files, or search.\n\n"
)


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
                model or judge_model(),
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


_API_MODEL_ID = re.compile(r"^claude-(opus|sonnet|haiku)-", re.I)


def acp_judge_model_key(model: str) -> str:
    """Turn the configured judge model into an ACP agent key.

    ``claude-code`` / ``claude-code:sonnet`` pass through. Anthropic API ids
    (``claude-sonnet-5``) map to Claude Code aliases — otherwise flipping the
    transport switch leaves an id the bridge 400s on, and every quality score
    fails. Bare aliases (``sonnet``, ``opus[1m]``, ``default``) are prefixed.
    """
    m = (model or "").strip()
    if not m:
        return "claude-code"
    base = m.partition(":")[0]
    if base in ("claude-code", "claude-acp"):
        return m
    match = _API_MODEL_ID.match(m)
    if match:
        return f"claude-code:{match.group(1).lower()}"
    return f"claude-code:{m}"


def judge_acp_workdir() -> Path:
    """Empty cwd for the judge ACP session.

    Must not be the condor checkout: ACP auto-discovers ``.mcp.json`` there, and
    a judge that can call hummingbot tools is no longer a judge.
    """
    path = ROOT / ".judge-acp"
    path.mkdir(parents=True, exist_ok=True)
    return path


class ClaudeJudge:
    def __init__(self, model: str | None = None, backend: str | None = None) -> None:
        self._model_override = model
        self._backend_override = backend
        self._client: anthropic.Anthropic | None = None
        self._async_client: anthropic.AsyncAnthropic | None = None
        self._acp: Any = None
        self._acp_key: str | None = None
        self._acp_tokens: tuple[int, int] = (0, 0)
        self._acp_lock = asyncio.Lock()

    @property
    def model_name(self) -> str:
        return self._model_override or judge_model()

    def effective_backend(self) -> str:
        if self._backend_override:
            raw = self._backend_override.strip().lower()
            if raw in (JUDGE_BACKEND_ACP, "claude-code", "claude-acp"):
                return JUDGE_BACKEND_ACP
            return "api"
        return judge_backend()

    def generate(self, prompt: str) -> str:
        if self.effective_backend() == JUDGE_BACKEND_ACP:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(self.a_generate(prompt))
            raise RuntimeError(
                "ACP judge cannot use generate() inside a running event loop; "
                "call a_generate() instead"
            )
        msg = self._api_clients()[0].messages.create(
            model=self.model_name,
            # The verdict itself is ~80 tokens of JSON. The rest is headroom for
            # models that emit extended thinking before answering: at 512 a long
            # transcript could consume the whole budget on thinking blocks and
            # return no text at all, which scored the case 0 on quality.
            max_tokens=JUDGE_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        _record(msg)
        return _text_of(msg)

    async def a_generate(self, prompt: str) -> str:
        if self.effective_backend() == JUDGE_BACKEND_ACP:
            return await self._acp_generate(prompt)
        if self._acp is not None:
            await self.aclose()
        msg = await self._api_clients()[1].messages.create(
            model=self.model_name,
            # The verdict itself is ~80 tokens of JSON. The rest is headroom for
            # models that emit extended thinking before answering: at 512 a long
            # transcript could consume the whole budget on thinking blocks and
            # return no text at all, which scored the case 0 on quality.
            max_tokens=JUDGE_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        _record(msg)
        return _text_of(msg)

    def _api_clients(self) -> tuple[anthropic.Anthropic, anthropic.AsyncAnthropic]:
        if self._client is None or self._async_client is None:
            key = os.environ.get("ANTHROPIC_API_KEY")
            self._client = anthropic.Anthropic(api_key=key)
            self._async_client = anthropic.AsyncAnthropic(api_key=key)
        return self._client, self._async_client

    async def _acp_generate(self, prompt: str) -> str:
        from condor_compat.acp.client import PromptDone, TextChunk, UsageEvent, fold_usage_event

        async with self._acp_lock:
            client = await self._ensure_acp()
            text_parts: list[str] = []
            usage_acc: dict[str, Any] = {}
            async for event in client.prompt_stream(_ACP_JUDGE_PREFIX + prompt):
                if isinstance(event, TextChunk):
                    text_parts.append(event.text)
                elif isinstance(event, UsageEvent):
                    fold_usage_event(usage_acc, event)
                elif isinstance(event, PromptDone):
                    if event.error or event.stop_reason in (
                        "error",
                        "timeout",
                        "disconnected",
                    ):
                        detail = event.error or event.stop_reason
                        raise RuntimeError(
                            f"ACP judge failed ({event.stop_reason}): {detail}"
                        )
            text = "".join(text_parts).strip()
            if not text:
                raise ValueError("judge response carried no text block (ACP)")
            self._record_acp_usage(usage_acc)
            return text

    async def _ensure_acp(self) -> Any:
        from condor_compat.acp.acp_client import ACPClient, resolve_acp

        key = acp_judge_model_key(self.model_name)
        if self._acp is not None and self._acp_key == key and self._acp.alive:
            return self._acp
        await self.aclose()
        command, extra_env = resolve_acp(key)
        client = ACPClient(
            command=command,
            mcp_servers=[],
            extra_env=extra_env or None,
            working_dir=str(judge_acp_workdir()),
        )
        try:
            await client.start()
        except Exception as exc:
            tail = client.stderr_tail()
            raise RuntimeError(
                f"ACP judge bridge `{command}` failed to start: {exc}"
                + (f"\n--- ACP stderr ---\n{tail}" if tail else "")
            ) from exc
        self._acp = client
        self._acp_key = key
        self._acp_tokens = (0, 0)
        return client

    def _record_acp_usage(self, acc: dict[str, Any]) -> None:
        # ACP usage is session-cumulative; JUDGE_USAGE wants per-call deltas.
        new_in = int(acc.get("input_tokens") or 0)
        new_out = int(acc.get("output_tokens") or 0)
        prev_in, prev_out = self._acp_tokens
        delta_in = max(0, new_in - prev_in)
        delta_out = max(0, new_out - prev_out)
        self._acp_tokens = (new_in, new_out)
        if delta_in or delta_out:
            JUDGE_USAGE.add(delta_in, delta_out)

    async def aclose(self) -> None:
        if self._acp is None:
            return
        try:
            await self._acp.stop()
        except Exception:
            pass
        self._acp = None
        self._acp_key = None
        self._acp_tokens = (0, 0)


def _text_of(msg: object) -> str:
    """Join the text blocks of a response, ignoring non-text ones.

    This used to be ``msg.content[0].text``, which assumed the first block is
    text. On a model that returns extended thinking the first block is a
    ``ThinkingBlock`` with no ``.text``, so every judge call raised — and
    ``AnswerQualityMetric`` catches judge errors and returns 0.0, so the whole
    suite silently scored zero quality while looking like it ran fine. Quality is
    ~half the composite, so that is not a small wrong number; it is every model
    failing every case for a reason unrelated to the model.
    """
    parts = [
        block.text
        for block in (getattr(msg, "content", None) or [])
        if getattr(block, "type", None) == "text" and getattr(block, "text", None)
    ]
    if parts:
        return "".join(parts)
    # No text block at all — surface it rather than returning "" and letting the
    # JSON parse fail with a confusing message.
    kinds = [getattr(b, "type", "?") for b in (getattr(msg, "content", None) or [])]
    raise ValueError(
        f"judge response carried no text block (blocks: {kinds or 'none'})"
    )


def _record(msg: object) -> None:
    usage = getattr(msg, "usage", None)
    if usage is None:
        return
    JUDGE_USAGE.add(
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
    )
