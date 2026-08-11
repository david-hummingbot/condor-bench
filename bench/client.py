"""Benchmark client.

Routes each case to the correct agent stack (PydanticAIClient or ACPClient),
attaches condor's real MCP servers, and captures events, tool traces, latency and
token usage. Multi-turn cases loop prompt_stream() calls preserving message
history.

Three things are captured per case:

* **Tool arguments and responses**, not just names. Param correctness and live
  response validity are what separate "picked the right tool" from "actually got
  the data", which is the distinction a model-sizing study lives on.
* **Token usage**, via ``UsageEvent``. Session-cumulative, so a multi-turn case
  reports the whole case.
* **Wiring metadata** — ``agent_slug``, resolved API URL, effective tool count —
  so a bad row can be identified as a harness artifact instead of being averaged
  into a routing recommendation.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from condor_compat.acp.pydantic_ai_client import PydanticAIClient
from condor_compat.acp.acp_client import ACPClient, is_acp_model, resolve_acp
from condor_compat.acp.client import (
    TextChunk,
    ToolCallEvent,
    ToolCallUpdate,
    UsageEvent,
    fold_usage_event,
)

from bench.mcp_provider import build_mcp_configs, wiring_metadata
from bench.tool_digest import DEFAULT_DIGEST_CHARS, digest_tool_output
from config import condor_path

# ── Agent instructions ─────────────────────────────────────────────────────────
_AGENT_MD = Path(__file__).parent.parent / "condor_compat" / "agents" / "condor" / "AGENT.md"
_AGENT_INSTRUCTIONS: str = _AGENT_MD.read_text() if _AGENT_MD.exists() else ""

_CONFIRM_RE = re.compile(
    r"(shall i|do you want(?: me)? to|please confirm|confirm (?:with|before|deploy|to)|"
    r"yes/no|reply (?:with )?(?:yes|confirm)|\bproceed\?\b|want me to (?:create|deploy|run))",
    re.I,
)
_AUTO_CONFIRM_MSG = "Yes, proceed. Deploy / execute with the parameters you proposed."


def _build_consult_prompt(question: str, instructions: str | None = None) -> str:
    """Mirror production build_agent_context(): instructions + [CONSULT REQUEST]."""
    parts = []
    prompt_body = instructions if instructions is not None else _AGENT_INSTRUCTIONS
    if prompt_body:
        parts.append(prompt_body)
    parts.append(f"[CONSULT REQUEST]\n{question}")
    return "\n\n".join(parts)


def load_assistant_prompt(slug: str | None) -> str:
    """System prompt for an assistant / agent, from the condor checkout.

    Ported from condor-evals' ``load_assistant_prompt()`` — the one piece of that
    harness worth keeping. Layer 3 cases are only meaningful if the model is given
    the same instructions production gives it: grading ``solana_dex_lp_expert``
    against the generic Condor prompt measures nothing about that agent.

    Falls back to the vendored Condor prompt (with a note in the result metadata
    via :func:`assistant_prompt_source`) so a missing checkout degrades to a
    weaker but still-runnable case rather than an exception.
    """
    if not slug:
        return _AGENT_INSTRUCTIONS
    repo = condor_path()
    if repo is None:
        return _AGENT_INSTRUCTIONS
    for candidate in (
        repo / "agents" / slug / "AGENT.md",
        repo / "assistants" / slug / "AGENT.md",
        repo / "assistants" / f"{slug}.md",
    ):
        if candidate.is_file():
            return _strip_frontmatter(candidate.read_text())
    return _AGENT_INSTRUCTIONS


def assistant_prompt_source(slug: str | None) -> str:
    """Where :func:`load_assistant_prompt` got its text, for results metadata."""
    if not slug:
        return "vendored:condor/AGENT.md"
    repo = condor_path()
    if repo is None:
        return "fallback:vendored (no condor checkout — prompt is NOT the agent's)"
    for candidate in (
        repo / "agents" / slug / "AGENT.md",
        repo / "assistants" / slug / "AGENT.md",
        repo / "assistants" / f"{slug}.md",
    ):
        if candidate.is_file():
            return f"condor:{candidate.relative_to(repo)}"
    return f"fallback:vendored (no prompt found for '{slug}')"


def _agent_md(slug: str) -> Path | None:
    """The AGENT.md condor would load for a slug, or None."""
    repo = condor_path()
    if repo is None:
        return None
    for candidate in (
        repo / "agents" / slug / "AGENT.md",
        repo / "assistants" / slug / "AGENT.md",
        repo / "assistants" / f"{slug}.md",
    ):
        if candidate.is_file():
            return candidate
    return None


def load_agent_tools(slug: str | None) -> list[str] | None:
    """The tool grant condor gives this agent, or None for the full surface.

    condor scopes an agent to its declared tools — ``allowed_tools=agent.tools or
    None`` in ``runtime/sessions.py``, ``agents/consult.py`` and
    ``agents/engine.py``. Benchmarking a specialist against all 24 tools when
    production offers it 8 measures a harder task than the one it does, and the
    bench-side ``_TOOL_LIMITS`` cap makes that worse rather than merely stricter:
    the cut is ``tool_defs[:limit]`` over whatever was discovered, so a small
    model on a ``market_making_expert`` case can be handed six tools that don't
    include ``manage_executors`` and fail for a tool it was never shown.

    ``None`` means two different things and the caller must not conflate them:
    no slug (chat-scoped), or an agent that declares no ``tools:`` key and
    therefore legitimately inherits everything — which is how ``condor``,
    ``directional_trader`` and ``smart_money_flow`` are defined. Both cases want
    no allowlist, so returning None for each is correct here; the distinction is
    recorded separately in wiring metadata via :func:`agent_tool_scope`.
    """
    if not slug:
        return None
    path = _agent_md(slug)
    if path is None:
        return None
    meta = _frontmatter(path.read_text())
    tools = meta.get("tools")
    if not isinstance(tools, list):
        return None
    names = [str(t).strip() for t in tools if str(t).strip()]
    return names or None


def agent_tool_scope(slug: str | None, allowed: list[str] | None) -> str:
    """Why this case's tool set is what it is, for results metadata."""
    if not slug:
        return "chat_scoped"
    if allowed:
        return "granted"
    if _agent_md(slug) is None:
        return "no_agent_md"
    return "full_surface"


def _frontmatter(text: str) -> dict[str, Any]:
    """Parse the leading YAML block of an AGENT.md. ``{}`` when absent or broken."""
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        import yaml  # noqa: PLC0415

        data = yaml.safe_load(parts[1])
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[2].lstrip("\n")
    return text


def _asks_confirmation(text: str) -> bool:
    return bool(_CONFIRM_RE.search(text or ""))


def _compact(value: Any, limit: int) -> str:
    """One-line, length-capped rendering of tool args or a tool response."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, default=str)
        except (TypeError, ValueError):
            text = str(value)
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _missing_required_tools(called: list[str], required: list[str]) -> list[str]:
    from metrics.tool_accuracy import normalize_tool_name

    seen = {normalize_tool_name(t) for t in called}
    return [t for t in required if normalize_tool_name(t) not in seen]


# ── Result types ───────────────────────────────────────────────────────────────
@dataclass
class TurnResult:
    response: str
    tool_calls: list[dict[str, Any]]
    latency_s: float
    error: str | None = None
    # Captured tool responses: [{"tool": name, "output": str, "tool_call_id": id}]
    tool_responses: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class BenchmarkResult:
    case_id: str
    model: str
    turns: list[TurnResult]          # one entry per conversation turn
    error: str | None = None
    # Session-cumulative token usage; {} when the provider reported none.
    usage: dict[str, Any] = field(default_factory=dict)
    # agent_slug / resolved URL / effective tool count for this case.
    wiring: dict[str, Any] = field(default_factory=dict)
    # Post-condition probe rows (see bench/post_conditions.py). Empty when the case
    # declares none; a row with score None means the probe could not run.
    post_conditions: list[dict[str, Any]] = field(default_factory=list)

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
    def tool_responses(self) -> list[dict[str, Any]]:
        responses: list[dict] = []
        for t in self.turns:
            responses.extend(t.tool_responses)
        return responses

    @property
    def latency_s(self) -> float:
        return sum(t.latency_s for t in self.turns)

    def tool_names(self) -> list[str]:
        return [c["tool"] for c in self.tool_calls]

    def transcript_for_judge(self, *, output_chars: int = DEFAULT_DIGEST_CHARS) -> str:
        """Full transcript with the tool log, for the quality judge.

        Tool *outputs* are included, not just names. The judge is instructed to
        penalise fabricated tool results heavily, and without the outputs it cannot
        tell a figure the tool returned from one the model invented — so it
        defaults to assuming the latter and marks down every correct, well-grounded
        answer whose content came from a tool. That penalty lands hardest on the
        Layer 2 tool cases, where the answer *is* the tool's data.

        Outputs are passed through :func:`digest_tool_output` rather than a raw
        head-truncate. Live portfolios (and similar long payloads) bury totals and
        valued holdings under dust rows; a digest keeps the citeable facts so the
        judge can verify grounding without stuffing the full dump into context.
        """
        if not self.turns:
            return ""
        if len(self.turns) == 1 and not self.turns[0].tool_calls:
            return self.turns[0].response

        outputs_by_id = {
            r.get("tool_call_id"): r.get("output")
            for r in self.tool_responses
            if r.get("tool_call_id")
        }
        # Fall back to name-based pairing: some providers don't echo call ids.
        outputs_by_name: dict[str, list[Any]] = {}
        for record in self.tool_responses:
            outputs_by_name.setdefault(str(record.get("tool", "")), []).append(
                record.get("output")
            )

        parts: list[str] = []
        for i, turn in enumerate(self.turns, 1):
            # The response comes first, before the tool log. The judge's prompt caps
            # the transcript it is shown (answer_quality._build_prompt), and the tool
            # log is unbounded in practice: eight calls at the digest budget produce
            # ~13k characters, which pushed the answer past the cap entirely. The
            # judge then reported "cuts off before any actual response" and scored a
            # complete, correct answer 0.15. Ordering the answer first makes that
            # failure impossible however long the tool log runs.
            lines = [f"--- Turn {i} ---", f"Response:\n{turn.response}"]
            if not turn.tool_calls:
                lines.append("Tools called: (none)")
            else:
                lines.append("Tool log:")
                for call in turn.tool_calls:
                    name = str(call.get("tool", ""))
                    args = call.get("args") or {}
                    output = outputs_by_id.get(call.get("tool_call_id"))
                    if output is None:
                        queue = outputs_by_name.get(name)
                        output = queue.pop(0) if queue else None
                    lines.append(f"  {name}({_compact(args, 300)})")
                    if output is None:
                        lines.append("    → (no output captured)")
                    else:
                        digest = digest_tool_output(
                            name, output, max_chars=output_chars
                        )
                        # Indent multi-line digests so they stay under the tool entry.
                        digest_lines = digest.splitlines() or [digest]
                        indented = "\n".join(
                            f"    → {ln}" if idx == 0 else f"      {ln}"
                            for idx, ln in enumerate(digest_lines)
                        )
                        lines.append(indented)
            parts.append("\n".join(lines))

        if self.error:
            parts.append(f"--- Error ---\n{self.error}")
        return "\n\n".join(parts)


# ── Internal helpers ───────────────────────────────────────────────────────────
async def _stream_turn(
    client: Any, prompt: str, usage_acc: dict[str, Any]
) -> TurnResult:
    """Send one prompt, collect text + tool events + usage, return a TurnResult."""
    text_chunks: list[str] = []
    tool_calls: list[dict] = []
    tool_responses: list[dict] = []
    # tool_call_id → tool name, so a ToolCallUpdate's output can be attributed.
    call_names: dict[str, str] = {}
    error: str | None = None
    t0 = time.monotonic()
    try:
        async for event in client.prompt_stream(prompt):
            if isinstance(event, TextChunk):
                text_chunks.append(event.text)
            elif isinstance(event, ToolCallEvent):
                call_names[event.tool_call_id] = event.title
                tool_calls.append(
                    {
                        "tool": event.title,
                        "args": event.input or {},
                        "tool_call_id": event.tool_call_id,
                        "status": event.status,
                    }
                )
            elif isinstance(event, ToolCallUpdate):
                if event.output is None:
                    continue
                tool_responses.append(
                    {
                        "tool": call_names.get(event.tool_call_id)
                        or event.title
                        or "unknown",
                        "tool_call_id": event.tool_call_id,
                        "output": event.output,
                        "status": event.status,
                    }
                )
            elif isinstance(event, UsageEvent):
                # Cumulative totals: fold (last non-None wins), never sum.
                fold_usage_event(usage_acc, event)
    except Exception as exc:
        error = str(exc)
    return TurnResult(
        response="".join(text_chunks),
        tool_calls=tool_calls,
        latency_s=time.monotonic() - t0,
        error=error,
        tool_responses=tool_responses,
    )


def _make_client(
    model: str,
    mcp_configs: list[dict],
    *,
    tool_filter_mode: str | None = None,
    allowed_tools: list[str] | None = None,
) -> tuple[Any, bool]:
    """Build the right client for a model key. Returns (client, is_acp).

    ``allowed_tools`` scopes the model to an agent's grant the way production does.
    The ACP path cannot take it — ACP agents resolve their own tools from the MCP
    servers in their working directory — so an ACP run of an agent-scoped case sees
    the full surface regardless. That is recorded in wiring metadata rather than
    silently tolerated, because it makes those rows measure a different task.
    """
    if is_acp_model(model):
        command, extra_env = resolve_acp(model)
        # ACP agents auto-discover stdio MCP servers from .mcp.json in their cwd.
        # That cwd must be the condor repo (that is where the real servers resolve
        # from), which also means condor's playwright entry is visible to the
        # agent — reported in wiring metadata, since the by-name overrides can
        # replace servers but not remove one.
        repo = condor_path()
        working_dir = str(repo) if repo else None
        return (
            ACPClient(
                command=command,
                mcp_servers=mcp_configs,
                extra_env=extra_env or None,
                working_dir=working_dir,
            ),
            True,
        )
    kwargs: dict[str, Any] = {}
    if tool_filter_mode:
        kwargs["tool_filter_mode"] = tool_filter_mode
    if allowed_tools:
        kwargs["allowed_tools"] = list(allowed_tools)
    return PydanticAIClient(model=model, mcp_servers=mcp_configs, **kwargs), False


# ── Public API ─────────────────────────────────────────────────────────────────
async def run_consult(
    case_id: str,
    question: str,
    model: str,
    extra_turns: list[str] | None = None,
    *,
    auto_confirm: bool = True,
    required_tools: list[str] | None = None,
    agent_slug: str | None = None,
    instructions: str | None = None,
    tool_filter_mode: str | None = None,
) -> BenchmarkResult:
    """Run a consult / tool / agent case with MCP tools available, as production does.

    extra_turns: additional user messages after the initial question (multi-turn).
    auto_confirm: if the model asks for confirmation and required tools are still
      missing, send one follow-up "Yes, proceed" turn (covers strategy-creation stalls).
    agent_slug: None keeps the run chat-scoped (a production consult); a slug
      scopes condor's memory/skill tools to that agent's own stores.
    """
    allowed_tools = load_agent_tools(agent_slug)
    all_turns: list[TurnResult] = []
    usage_acc: dict[str, Any] = {}
    outer_error: str | None = None
    mcp_configs: list[dict] = []
    is_acp = False
    client: Any = None

    try:
        mcp_configs = build_mcp_configs(agent_slug=agent_slug)
        client, is_acp = _make_client(
            model,
            mcp_configs,
            tool_filter_mode=tool_filter_mode,
            allowed_tools=allowed_tools,
        )

        await client.start()
        try:
            first_prompt = _build_consult_prompt(question, instructions)
            turn = await _stream_turn(client, first_prompt, usage_acc)
            all_turns.append(turn)
            if turn.error:
                outer_error = turn.error
            else:
                for follow_up in (extra_turns or []):
                    turn = await _stream_turn(client, follow_up, usage_acc)
                    all_turns.append(turn)
                    if turn.error:
                        outer_error = turn.error
                        break

                # Auto-confirm when the model gated a fully-specified action
                if auto_confirm and not outer_error and required_tools and all_turns:
                    called = [c["tool"] for t in all_turns for c in t.tool_calls]
                    missing = _missing_required_tools(called, required_tools)
                    if missing and _asks_confirmation(all_turns[-1].response):
                        turn = await _stream_turn(client, _AUTO_CONFIRM_MSG, usage_acc)
                        all_turns.append(turn)
                        if turn.error:
                            outer_error = turn.error
        finally:
            await client.stop()
    except Exception as exc:
        outer_error = str(exc)
        if not all_turns:
            all_turns.append(TurnResult(response="", tool_calls=[], latency_s=0.0, error=str(exc)))

    return BenchmarkResult(
        case_id=case_id,
        model=model,
        turns=all_turns,
        error=outer_error,
        usage=usage_acc,
        wiring=wiring_metadata(
            mcp_configs,
            agent_slug=agent_slug,
            is_acp=is_acp,
            allowed_tools=allowed_tools,
            tool_scope=agent_tool_scope(agent_slug, allowed_tools),
            offered_tools=getattr(client, "offered_tools", None),
        )
        | {"assistant_prompt": assistant_prompt_source(agent_slug) if agent_slug else None},
    )


async def run_tick(
    case_id: str,
    prompt: str,
    model: str,
    *,
    agent_slug: str | None = None,
) -> BenchmarkResult:
    """Run a tick case with both MCP servers attached.

    Ticks are agent-scoped by construction — they run *as* a trading agent — so
    ``agent_slug`` should always be set. Without it condor's journal and memory
    tools write to the chat's stores, and a case asserting
    ``trading_agent_journal_write`` measures the wrong thing.
    """
    allowed_tools = load_agent_tools(agent_slug)
    all_turns: list[TurnResult] = []
    usage_acc: dict[str, Any] = {}
    outer_error: str | None = None
    mcp_configs: list[dict] = []
    is_acp = False
    client: Any = None

    try:
        mcp_configs = build_mcp_configs(agent_slug=agent_slug)
        # Ticks skip the model-size cap: production does not filter an agent's tools
        # by model size on the tick path, and a truncated set would make a tick
        # failure indistinguishable from a tool that was never offered. The agent's
        # own grant still applies when it declares one.
        client, is_acp = _make_client(
            model, mcp_configs, tool_filter_mode="full", allowed_tools=allowed_tools
        )

        await client.start()
        try:
            turn = await _stream_turn(client, prompt, usage_acc)
            all_turns.append(turn)
            if turn.error:
                outer_error = turn.error
        finally:
            await client.stop()
    except Exception as exc:
        outer_error = str(exc)
        all_turns.append(TurnResult(response="", tool_calls=[], latency_s=0.0, error=str(exc)))

    return BenchmarkResult(
        case_id=case_id,
        model=model,
        turns=all_turns,
        error=outer_error,
        usage=usage_acc,
        wiring=wiring_metadata(
            mcp_configs,
            agent_slug=agent_slug,
            is_acp=is_acp,
            allowed_tools=allowed_tools,
            tool_scope=agent_tool_scope(agent_slug, allowed_tools),
            offered_tools=getattr(client, "offered_tools", None),
        ),
    )


def build_tick_prompt_for_case(case: Any, model: str) -> str:
    from condor_compat.agents.prompts import build_tick_prompt
    agent = SimpleNamespace(
        name=case.id, agent_key=model, instructions=case.agent_instructions, tools=[],
    )
    slug = getattr(case, "agent_slug", None) or "bench"
    strategy = SimpleNamespace(
        key=f"bench.{case.id}", agent_slug=slug, instructions=case.strategy_instructions,
        agent_key=model, name=case.scenario_name,
    )
    return build_tick_prompt(
        agent=agent, strategy=strategy, config=case.config,
        core_data=case.core_data, learnings=case.learnings, summary=case.summary,
        recent_decisions=case.recent_decisions, risk_state=case.risk_state,
        tick_number=case.tick_number, agent_id=f"bench-{case.id}",
        cached_routines_section="", user_memory="", skills_index="",
    )


async def run_case(case: Any, model: str) -> BenchmarkResult:
    """Run any dataset case, dispatching on its type.

    One entry point so the CLI, the dashboard and the sweep runner cannot drift on
    which cases get an ``agent_slug`` or an assistant prompt.

    Post-conditions are verified here rather than by the caller, which puts them
    structurally before ``cleanup.teardown`` — teardown deletes exactly the
    artefacts a post-condition asserts exist, so the ordering cannot be left to
    each call site remembering it.
    """
    slug = getattr(case, "agent_slug", None)
    if case.type == "tick":
        prompt = build_tick_prompt_for_case(case, model)
        result = await run_tick(case.id, prompt, model, agent_slug=slug)
    else:
        expected = list(getattr(case, "expected_tools", []) or [])
        result = await run_consult(
            case.id,
            case.question,
            model,
            extra_turns=list(getattr(case, "turns", []) or []),
            required_tools=expected or None,
            agent_slug=slug,
            instructions=load_assistant_prompt(slug) if slug else None,
        )

    conditions = getattr(case, "post_conditions", {}) or {}
    if conditions and not result.error:
        from bench.post_conditions import verify  # noqa: PLC0415

        result.post_conditions = await verify(
            conditions, model=model, agent_slug=slug
        )
    return result


def case_input_text(case: Any) -> str:
    """The text the judge is shown as "what the user asked"."""
    return case.scenario_name if case.type == "tick" else getattr(case, "question", "")
