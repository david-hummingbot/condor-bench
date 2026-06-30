"""Event types emitted by condor's LLM clients.

Vendored from condor/condor/acp/client.py — only the dataclasses and type
aliases consumed by condor-bench. The full ACPClient (subprocess agent) is
not included since the benchmark uses PydanticAIClient exclusively.

Source: https://github.com/hummingbot/condor
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass
class TextChunk:
    text: str


@dataclass
class ThoughtChunk:
    text: str


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


@dataclass
class PromptDone:
    stop_reason: str


@dataclass
class Heartbeat:
    elapsed_seconds: float


ACPEvent = (
    TextChunk | ThoughtChunk | ToolCallEvent | ToolCallUpdate | PromptDone | Heartbeat
)

PermissionCallback = Callable[[dict, list[dict]], Awaitable[dict]]
