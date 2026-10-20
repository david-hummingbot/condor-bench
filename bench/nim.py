"""NVIDIA NIM (build.nvidia.com) as a model provider, and what it costs to trust it.

NIM's hosted endpoint is OpenAI-compatible, so ``nvidia:<catalog id>`` runs through
the same pydantic-ai path as ``openrouter:``. Three things make it different enough
to need this module:

* **Rate limits.** The free tier caps requests per minute. Bench runs NIM requests
  one at a time per process and lets the client retry with backoff, and records
  every 429 — a trial that spent half its wall clock in backoff is not a latency
  measurement.
* **Latency is not representative.** Shared hosted capacity, queueing and retries
  make NIM's wall clock a statement about the endpoint, not the model, so NIM rows
  carry ``latency_representative: False`` and stay out of every latency gate.
* **Evidence per trial.** HTTP status, request id, rate-limit events, a truncated
  raw response, usage and client latency are recorded for each call
  (:class:`CallLog`), so a failed trial can be told apart as model, quota or outage.

:func:`smoke` is the gate before a sweep: is the model listed, does it emit a real
tool call, and does it accept a bench-sized context. A model that fails any of the
three would only produce rows that measure the endpoint.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

NIM_PREFIX = "nvidia"
NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"
#: Retries the OpenAI client makes on 429/5xx, with its own exponential backoff.
NIM_MAX_RETRIES = 4
#: Raw-body bytes kept per call — enough to read an error, not a transcript.
RAW_RESPONSE_CHARS = 2000
#: Prompt size :func:`smoke` probes. A Condor case sends roughly 60–100k tokens of
#: tool schemas and instructions, so a model that cannot take this cannot run one.
SMOKE_CONTEXT_TOKENS = 64_000


def is_nim_model(model: str) -> bool:
    return (model or "").split(":", 1)[0] == NIM_PREFIX


def latency_representative(model: str) -> bool:
    """False for providers whose wall clock measures the endpoint, not the model."""
    return not is_nim_model(model)


def base_url() -> str:
    return (os.environ.get("NVIDIA_BASE_URL") or NIM_BASE_URL).rstrip("/")


def api_key() -> str:
    key = os.environ.get("NVIDIA_API_KEY") or ""
    if not key:
        raise RuntimeError("NVIDIA_API_KEY is not set. Add it to .env to use nvidia:* models.")
    return key


# ── Per-call recording ─────────────────────────────────────────────────────────
@dataclass
class CallLog:
    """One HTTP exchange with the NIM endpoint."""

    status: int
    request_id: str | None
    latency_s: float
    rate_limited: bool
    raw_response: str
    usage: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class CallRecorder:
    """httpx event hooks that log every NIM response onto a list.

    Attached to the ``httpx.AsyncClient`` the OpenAI SDK uses, so retries the SDK
    makes on its own are recorded too — they are exactly the rate-limit evidence.
    """

    def __init__(self) -> None:
        self.calls: list[CallLog] = []

    async def on_request(self, request: httpx.Request) -> None:
        request.extensions["bench_t0"] = time.monotonic()

    async def on_response(self, response: httpx.Response) -> None:
        t0 = response.request.extensions.get("bench_t0", time.monotonic())
        await response.aread()
        body = response.text
        usage: dict[str, Any] = {}
        try:
            payload = response.json()
            if isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
                usage = payload["usage"]
        except ValueError:
            pass
        self.calls.append(
            CallLog(
                status=response.status_code,
                request_id=response.headers.get("nvcf-reqid") or response.headers.get("x-request-id"),
                latency_s=round(time.monotonic() - t0, 3),
                rate_limited=response.status_code == 429,
                raw_response=body[:RAW_RESPONSE_CHARS],
                usage=usage,
            )
        )

    def http_client(self, timeout: httpx.Timeout) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=timeout,
            event_hooks={"request": [self.on_request], "response": [self.on_response]},
        )

    def summary(self) -> dict[str, Any]:
        statuses: dict[str, int] = {}
        for c in self.calls:
            statuses[str(c.status)] = statuses.get(str(c.status), 0) + 1
        return {
            "provider": "nvidia_nim",
            "requests": len(self.calls),
            "statuses": statuses,
            "rate_limit_events": sum(1 for c in self.calls if c.rate_limited),
            "request_ids": [c.request_id for c in self.calls if c.request_id],
            "client_latency_s": round(sum(c.latency_s for c in self.calls), 3),
            "calls": [c.as_dict() for c in self.calls],
            "latency_representative": False,
        }


# ── Smoke test ─────────────────────────────────────────────────────────────────
_SMOKE_TOOL = {
    "type": "function",
    "function": {
        "name": "get_prices",
        "description": "Get the latest price for trading pairs on a connector.",
        "parameters": {
            "type": "object",
            "properties": {
                "connector_name": {"type": "string"},
                "trading_pairs": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["connector_name", "trading_pairs"],
        },
    },
}


@dataclass
class SmokeResult:
    model: str
    listed: bool
    tool_call: bool
    context_ok: bool
    detail: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.listed and self.tool_call and self.context_ok

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ok": self.ok}


async def _chat(client: httpx.AsyncClient, payload: dict[str, Any]) -> httpx.Response:
    for attempt in range(NIM_MAX_RETRIES + 1):
        r = await client.post(f"{base_url()}/chat/completions", json=payload)
        if r.status_code != 429 or attempt == NIM_MAX_RETRIES:
            return r
        await asyncio.sleep(min(30.0, 2.0 ** attempt))
    return r  # pragma: no cover


async def smoke(model: str, *, context_tokens: int = SMOKE_CONTEXT_TOKENS) -> SmokeResult:
    """Listed? Real tool call? Bench-sized context? Each costs one request."""
    model_id = model.split(":", 1)[1] if is_nim_model(model) else model
    result = SmokeResult(model=model, listed=False, tool_call=False, context_ok=False)
    headers = {"Authorization": f"Bearer {api_key()}"}
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(180.0, connect=15.0)) as client:
        r = await client.get(f"{base_url()}/models")
        ids = {m.get("id") for m in (r.json().get("data") or [])} if r.status_code == 200 else set()
        result.listed = model_id in ids
        result.detail["listed"] = "in /models" if result.listed else f"not in /models (HTTP {r.status_code})"
        if not result.listed:
            return result

        r = await _chat(client, {
            "model": model_id,
            "messages": [{"role": "user", "content": "What is the BTC-USDT price on binance? Use the tool."}],
            "tools": [_SMOKE_TOOL],
            "tool_choice": "auto",
            "max_tokens": 512,
        })
        if r.status_code == 200:
            msg = (r.json().get("choices") or [{}])[0].get("message") or {}
            calls = msg.get("tool_calls") or []
            result.tool_call = any((c.get("function") or {}).get("name") == "get_prices" for c in calls)
            result.detail["tool_call"] = (
                "emitted get_prices" if result.tool_call
                else f"no tool call (content: {str(msg.get('content'))[:120]!r})"
            )
        else:
            result.detail["tool_call"] = f"HTTP {r.status_code}: {r.text[:200]}"

        # ~4 chars per token of neutral filler, then ask for one token back.
        filler = ("market data line: price steady, spread tight, volume normal. " * (context_tokens // 14))
        r = await _chat(client, {
            "model": model_id,
            "messages": [{"role": "user", "content": filler + "\nReply with OK."}],
            "max_tokens": 4,
        })
        result.context_ok = r.status_code == 200
        prompt_tokens = (r.json().get("usage") or {}).get("prompt_tokens") if r.status_code == 200 else None
        result.detail["context"] = (
            f"accepted {prompt_tokens} prompt tokens" if result.context_ok
            else f"HTTP {r.status_code}: {r.text[:200]}"
        )
    return result
