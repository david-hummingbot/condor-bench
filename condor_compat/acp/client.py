"""Event types emitted by condor's LLM clients.

Vendored from condor/condor/acp/client.py — only the dataclasses and type
aliases consumed by condor-bench. The full ACPClient (subprocess agent) is
not included since the benchmark uses PydanticAIClient exclusively.

Source: https://github.com/hummingbot/condor
"""

from __future__ import annotations

import json

from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass
class TextChunk:
    text: str


@dataclass
class ThoughtChunk:
    text: str


def unwrap_content_blocks(value: Any) -> str | None:
    """The text inside an MCP content-block list, or None if that's not what it is.

    Condor's MCP tools answer with the block form rather than a bare payload::

        [{"type": "text", "text": "{\\"server\\": \\"bench_staging\\", …}"}]

    Left wrapped, everything downstream sees the envelope instead of the result.
    The judge was shown ``[digest] json list / items: 1 items (e.g. keys: type,
    text)`` — literally no content — and then asked whether the answer was grounded
    in it, so a verbatim-correct answer was marked down as "partially fabricated"
    (c006 scored 0.55 with tools, params and validity all 1.0). ``live_expected``
    ``fields`` assertions have the same problem: they look for a key that is one
    level inside the envelope.

    Both block shapes seen on the wire are handled: ``{"type": "text", …}`` and the
    nested ``{"type": "content", "content": {"type": "text", …}}``.
    """
    if not isinstance(value, list) or not value:
        return None
    texts: list[str] = []
    for block in value:
        if not isinstance(block, dict):
            return None
        inner = block.get("content")
        if isinstance(inner, dict) and inner.get("type") == "text":
            texts.append(str(inner.get("text", "")))
        elif block.get("type") == "text":
            texts.append(str(block.get("text", "")))
        else:
            return None  # not a content-block list; leave it alone
    joined = "\n".join(t for t in texts if t)
    return joined or None


def stringify_tool_output(content: Any) -> str:
    """One wire shape for a tool result, whichever client captured it.

    The two transports disagreed about structured payloads. ACP delivers JSON; the
    PydanticAI path ran `str(content)` over a dict, producing a Python repr
    (``{'server': 'bench_staging'}``) that no JSON parser accepts. Everything that
    reads *into* a payload then behaved differently by transport — a
    ``live_expected`` ``fields`` assertion scored 1.0 on ACP and 0.5 on PydanticAI
    for the same tool returning the same data.
    """
    if isinstance(content, str):
        return content
    unwrapped = unwrap_content_blocks(content)
    if unwrapped is not None:
        return unwrapped
    try:
        return json.dumps(content, default=str)
    except (TypeError, ValueError):
        return str(content)


@dataclass
class ToolCallEvent:
    tool_call_id: str
    title: str
    status: str  # pending, in_progress, completed, failed, blocked
    kind: str = "other"
    input: dict | None = None


@dataclass
class ToolCallUpdate:
    tool_call_id: str
    status: str | None = None
    title: str | None = None
    output: str | None = None
    # Arguments, when the update is what carries them. Diverges from condor's copy
    # on purpose: claude-agent-acp announces a call with `rawInput: {}` and fills the
    # arguments in on a *later* tool_call_update, so a consumer that only reads the
    # opening frame records every call with no arguments. condor renders tool calls
    # and does not mind; bench scores their parameters, so it has to see them.
    input: dict | None = None


@dataclass
class PromptDone:
    stop_reason: str
    # Why the prompt failed, when it did. Diverges from condor's copy on purpose:
    # condor shows the user a chat that visibly went wrong, so a stop_reason is
    # enough. bench *scores* the turn, and a failed prompt that arrives as nothing
    # but stop_reason="error" is indistinguishable from a model that chose to say
    # nothing — it was recorded as an empty answer, judged "No response produced",
    # and blamed on the model. The bridge's own message lands here instead.
    error: str | None = None


@dataclass
class Heartbeat:
    elapsed_seconds: float


@dataclass
class UsageEvent:
    """Token/cost usage reported by the agent for the current session.

    Every field is optional because agents differ in what they report (the
    ``claude-agent-acp`` bridge sends the full breakdown; other ACP bridges may
    send nothing at all). ``None`` means "not reported" — distinct from ``0``,
    which would read as "free".

    Values are SESSION-CUMULATIVE, not per-prompt deltas: they cover every
    prompt this client has run. A benchmark case builds a fresh client, so the
    cumulative total *is* that case's usage — including its follow-up turns.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    context_used: int | None = None  # tokens occupying the context window
    context_size: int | None = None  # context window capacity
    # The model id the agent actually used, when it tells us (gemini names it in
    # its response). Lets a row be priced even though the configured agent_key is
    # an alias no price dataset knows.
    model: str | None = None


ACPEvent = (
    TextChunk
    | ThoughtChunk
    | ToolCallEvent
    | ToolCallUpdate
    | PromptDone
    | Heartbeat
    | UsageEvent
)

USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "total_tokens",
    "cost_usd",
    "context_used",
    "context_size",
    "model",
)


def fold_usage_event(acc: dict[str, Any], event: UsageEvent) -> dict[str, Any]:
    """Fold a :class:`UsageEvent` into ``acc`` in place and return it.

    LAST NON-NONE WINS — deliberately *not* a sum. Usage events carry
    session-cumulative totals, so a later event supersedes an earlier one rather
    than adding to it. A multi-turn case therefore ends with the whole case's
    usage, not its last turn's, and not a double count.
    """
    for field_name in USAGE_FIELDS:
        value = getattr(event, field_name, None)
        if value is not None:
            acc[field_name] = value
    # Derive the total only when an agent reports parts but no total. Summing all
    # four is right for the ACP bridge, whose fields are disjoint (it sums them
    # for its own totalTokens) — but NOT for pydantic-ai, whose convention nests
    # cached tokens inside input_tokens. That path always supplies its own total
    # (see PydanticAIClient._fold_run_usage), so it never reaches this branch.
    if acc.get("total_tokens") is None:
        parts = [
            acc.get(f)
            for f in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            )
        ]
        if any(p is not None for p in parts):
            acc["total_tokens"] = sum(p or 0 for p in parts)
    return acc


def parse_prompt_usage(raw: Any) -> UsageEvent | None:
    """Build a :class:`UsageEvent` from a ``session/prompt`` response's ``usage``.

    Returns ``None`` when the agent reported no usage (older bridges, or ACP
    agents that don't implement it) so callers can tell "unreported" from zero.
    """
    if not isinstance(raw, dict):
        return None

    def _int(key: str) -> int | None:
        value = raw.get(key)
        return int(value) if isinstance(value, (int, float)) else None

    event = UsageEvent(
        input_tokens=_int("inputTokens"),
        output_tokens=_int("outputTokens"),
        cache_read_tokens=_int("cachedReadTokens"),
        cache_write_tokens=_int("cachedWriteTokens"),
        total_tokens=_int("totalTokens"),
    )
    if all(getattr(event, f) is None for f in USAGE_FIELDS):
        return None
    return event


def parse_meta_usage(meta: Any) -> UsageEvent | None:
    """Build a :class:`UsageEvent` from a prompt response's ``_meta`` extension.

    ACP puts vendor extensions in ``_meta``, and that is where the Gemini CLI
    reports tokens — ``_meta.quota.token_count`` with snake_case keys, cumulative
    across the session's turns. It sends no ``usage`` object and no
    ``usage_update`` notification, so without reading this a Gemini agent looks
    free — which in a cost comparison is worse than looking expensive.
    """
    if not isinstance(meta, dict):
        return None
    quota = meta.get("quota")
    if not isinstance(quota, dict):
        return None
    counts = quota.get("token_count")
    if not isinstance(counts, dict):
        return None

    def _int(key: str) -> int | None:
        value = counts.get(key)
        return int(value) if isinstance(value, (int, float)) else None

    input_tokens, output_tokens = _int("input_tokens"), _int("output_tokens")
    if input_tokens is None and output_tokens is None:
        return None

    model = None
    usages = quota.get("model_usage")
    if isinstance(usages, list):
        # Multiple models can serve one prompt (a fallback mid-turn). Name the
        # busiest rather than the first, so pricing follows the bulk of the work.
        best = 0
        for entry in usages:
            if not isinstance(entry, dict):
                continue
            entry_counts = entry.get("token_count")
            weight = 0
            if isinstance(entry_counts, dict):
                weight = (entry_counts.get("input_tokens") or 0) + (
                    entry_counts.get("output_tokens") or 0
                )
            if entry.get("model") and (model is None or weight > best):
                model, best = str(entry["model"]), weight

    return UsageEvent(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        # input + output only: Gemini reports no cached split, so there is nothing
        # to risk double-counting here.
        total_tokens=(input_tokens or 0) + (output_tokens or 0),
        model=model,
    )


def format_usage(usage: dict[str, Any] | None) -> str:
    """One-line human summary of a folded usage dict ("" when nothing reported)."""
    if not usage:
        return ""
    parts: list[str] = []
    total = usage.get("total_tokens")
    if total is not None:
        parts.append(f"{total:,} tokens")
    breakdown = [
        (usage.get("input_tokens"), "in"),
        (usage.get("output_tokens"), "out"),
        (usage.get("cache_read_tokens"), "cache-read"),
        (usage.get("cache_write_tokens"), "cache-write"),
    ]
    detail = ", ".join(f"{v:,} {label}" for v, label in breakdown if v is not None)
    if detail:
        parts.append(f"({detail})")
    cost = usage.get("cost_usd")
    if cost is not None:
        parts.append(f"| ${cost:.4f}")
    used, size = usage.get("context_used"), usage.get("context_size")
    if used is not None and size:
        parts.append(f"| context {used:,}/{size:,} ({used / size:.0%})")
    return " ".join(parts)


PermissionCallback = Callable[[dict, list[dict]], Awaitable[dict]]
