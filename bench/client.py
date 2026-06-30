"""Benchmark client.

Routes each case to the correct agent stack (PydanticAIClient or ACPClient),
attaches both mock MCP servers, and captures events + latency.
Multi-turn cases loop prompt_stream() calls preserving message history.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from condor_compat.acp.pydantic_ai_client import PydanticAIClient
from condor_compat.acp.acp_client import ACPClient, is_acp_model, resolve_acp
from condor_compat.acp.client import TextChunk, ToolCallEvent

# ── Agent instructions ─────────────────────────────────────────────────────────
_AGENT_MD = Path(__file__).parent.parent / "condor_compat" / "assistants" / "condor" / "AGENT.md"
_AGENT_INSTRUCTIONS: str = _AGENT_MD.read_text() if _AGENT_MD.exists() else ""

# ── Mock MCP server paths ──────────────────────────────────────────────────────
_ROOT = Path(__file__).parent.parent
_HB_SCRIPT = _ROOT / "mock_mcp" / "hummingbot_server.py"
_CONDOR_SCRIPT = _ROOT / "mock_mcp" / "condor_server.py"


def _build_consult_prompt(question: str) -> str:
    """Mirror production build_agent_context(): instructions + [CONSULT REQUEST]."""
    parts = []
    if _AGENT_INSTRUCTIONS:
        parts.append(_AGENT_INSTRUCTIONS)
    parts.append(f"[CONSULT REQUEST]\n{question}")
    return "\n\n".join(parts)


def _mock_mcp_configs(scenario_path: str, tool_log_path: str) -> list[dict]:
    """Return two named MCP server configs mirroring production (hummingbot + condor)."""
    env = [
        {"name": "BENCH_SCENARIO_FILE", "value": scenario_path},
        {"name": "BENCH_TOOL_LOG", "value": tool_log_path},
    ]
    py = sys.executable

    def _cmd(script: Path) -> list[str]:
        return [str(py), str(script)] if script.exists() else [str(py), "-m", f"mock_mcp.{script.stem}"]

    return [
        {"name": "mcp-hummingbot", "command": _cmd(_HB_SCRIPT)[0], "args": _cmd(_HB_SCRIPT)[1:], "env": env},
        {"name": "condor",         "command": _cmd(_CONDOR_SCRIPT)[0], "args": _cmd(_CONDOR_SCRIPT)[1:], "env": env},
    ]


# ── Result types ───────────────────────────────────────────────────────────────
@dataclass
class TurnResult:
    response: str
    tool_calls: list[dict[str, Any]]
    latency_s: float
    error: str | None = None


@dataclass
class BenchmarkResult:
    case_id: str
    model: str
    turns: list[TurnResult]          # one entry per conversation turn
    error: str | None = None

    @property
    def response(self) -> str:
        """Final turn response (for single-turn compat)."""
        return self.turns[-1].response if self.turns else ""

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        """All tool calls across all turns."""
        calls: list[dict] = []
        for t in self.turns:
            calls.extend(t.tool_calls)
        return calls

    @property
    def latency_s(self) -> float:
        return sum(t.latency_s for t in self.turns)

    def tool_names(self) -> list[str]:
        return [c["tool"] for c in self.tool_calls]


# ── Internal helpers ───────────────────────────────────────────────────────────
async def _stream_turn(client: Any, prompt: str) -> TurnResult:
    """Send one prompt, collect text + tool events, return a TurnResult."""
    text_chunks: list[str] = []
    tool_calls: list[dict] = []
    error: str | None = None
    t0 = time.monotonic()
    try:
        async for event in client.prompt_stream(prompt):
            if isinstance(event, TextChunk):
                text_chunks.append(event.text)
            elif isinstance(event, ToolCallEvent):
                tool_calls.append({"tool": event.title, "args": event.input or {}})
    except Exception as exc:
        error = str(exc)
    return TurnResult(
        response="".join(text_chunks),
        tool_calls=tool_calls,
        latency_s=time.monotonic() - t0,
        error=error,
    )


def _read_tool_log(path: str) -> list[dict]:
    try:
        records = []
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
        return records
    except Exception:
        return []


# ── Public API ─────────────────────────────────────────────────────────────────
async def run_consult(
    case_id: str,
    question: str,
    model: str,
    extra_turns: list[str] | None = None,
    mock_tools: dict[str, Any] | None = None,
) -> BenchmarkResult:
    """Run a consult case (with MCP tools available, same as production).

    extra_turns: additional user messages after the initial question (multi-turn).
    """
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as sf:
        json.dump({"mock_tools": mock_tools or {}}, sf)
        scenario_path = sf.name
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as lf:
        tool_log_path = lf.name

    mcp_configs = _mock_mcp_configs(scenario_path, tool_log_path)
    all_turns: list[TurnResult] = []
    outer_error: str | None = None

    try:
        if is_acp_model(model):
            command, extra_env = resolve_acp(model)
            client: Any = ACPClient(command=command, mcp_servers=mcp_configs, extra_env=extra_env or None)
        else:
            client = PydanticAIClient(model=model, mcp_servers=mcp_configs)

        await client.start()
        try:
            first_prompt = _build_consult_prompt(question)
            turn = await _stream_turn(client, first_prompt)
            all_turns.append(turn)
            if turn.error:
                outer_error = turn.error
            else:
                for follow_up in (extra_turns or []):
                    turn = await _stream_turn(client, follow_up)
                    all_turns.append(turn)
                    if turn.error:
                        outer_error = turn.error
                        break
        finally:
            await client.stop()
    except Exception as exc:
        outer_error = str(exc)
        if not all_turns:
            all_turns.append(TurnResult(response="", tool_calls=[], latency_s=0.0, error=str(exc)))

    # Prefer the MCP tool log (has full args + results) over pydantic-ai events
    logged = _read_tool_log(tool_log_path)
    if logged:
        # Redistribute to the turns proportionally (approximate — log order matches call order)
        for t in all_turns:
            t.tool_calls = []
        for rec in logged:
            for t in reversed(all_turns):
                t.tool_calls.append({"tool": rec["tool"], "args": rec.get("args", {})})
                break

    for p in (scenario_path, tool_log_path):
        try:
            Path(p).unlink()
        except Exception:
            pass

    return BenchmarkResult(case_id=case_id, model=model, turns=all_turns, error=outer_error)


async def run_tick(
    case_id: str,
    prompt: str,
    model: str,
    mock_tools: dict[str, Any] | None = None,
) -> BenchmarkResult:
    """Run a tick case with both mock MCP servers attached."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as sf:
        json.dump({"mock_tools": mock_tools or {}}, sf)
        scenario_path = sf.name
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as lf:
        tool_log_path = lf.name

    mcp_configs = _mock_mcp_configs(scenario_path, tool_log_path)
    all_turns: list[TurnResult] = []
    outer_error: str | None = None

    try:
        if is_acp_model(model):
            command, extra_env = resolve_acp(model)
            client: Any = ACPClient(command=command, mcp_servers=mcp_configs, extra_env=extra_env or None)
        else:
            client = PydanticAIClient(model=model, mcp_servers=mcp_configs, tool_filter_mode="full")

        await client.start()
        try:
            turn = await _stream_turn(client, prompt)
            all_turns.append(turn)
            if turn.error:
                outer_error = turn.error
        finally:
            await client.stop()
    except Exception as exc:
        outer_error = str(exc)
        all_turns.append(TurnResult(response="", tool_calls=[], latency_s=0.0, error=str(exc)))

    logged = _read_tool_log(tool_log_path)
    if logged and all_turns:
        all_turns[0].tool_calls = [{"tool": r["tool"], "args": r.get("args", {})} for r in logged]

    for p in (scenario_path, tool_log_path):
        try:
            Path(p).unlink()
        except Exception:
            pass

    return BenchmarkResult(case_id=case_id, model=model, turns=all_turns, error=outer_error)


def build_tick_prompt_for_case(case: Any, model: str) -> str:
    from condor_compat.agents.prompts import build_tick_prompt
    agent = SimpleNamespace(
        name=case.id, agent_key=model, instructions=case.agent_instructions, tools=[],
    )
    strategy = SimpleNamespace(
        key=f"bench.{case.id}", agent_slug="bench", instructions=case.strategy_instructions,
        agent_key=model, name=case.scenario_name,
    )
    return build_tick_prompt(
        agent=agent, strategy=strategy, config=case.config,
        core_data=case.core_data, learnings=case.learnings, summary=case.summary,
        recent_decisions=case.recent_decisions, risk_state=case.risk_state,
        tick_number=case.tick_number, agent_id=f"bench-{case.id}",
        cached_routines_section="", user_memory="", skills_index="",
    )
