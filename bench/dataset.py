"""Benchmark dataset types and loaders.

Four layers, all loaded through :func:`load_all_cases`:

* ``consult`` — end-to-end advisory + strategy-creation cases (Layer 1)
* ``tick``    — simulated agent ticks (Layer 1, agent-scoped)
* ``tool``    — one focused case per MCP tool (Layer 2)
* ``agent``   — cases routed to a specific Condor assistant (Layer 3)

Two fields drive the live-mode machinery and are worth reading before adding a
case:

``agent_slug``
    Which condor store the MCP tools bind to. ``None`` means chat-scoped, which
    is what a production consult does. Anything acting *as* an agent — ticks,
    Layer 3 cases — must name its slug, or condor's memory/skill tools read the
    chat's stores and the case fails for a harness reason (see
    ``bench/mcp_provider.py``).

``risk_level``
    ``read_only`` | ``mutating`` | ``destructive``. Every case runs — isolation is
    the API instance's job, not a flag here. What the level still decides is the
    score bar: ``destructive`` cases must clear ``DESTRUCTIVE_FLOOR`` before a
    model can own the domain, and ``is_mutating`` decides whether teardown runs
    after the case. Unset defaults to ``read_only``, so a case that *does* mutate
    must say so explicitly or it will not be cleaned up.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from config import DATASETS_DIR

RISK_LEVELS = ("read_only", "mutating", "destructive")

# Category → routing domain, for consult categories that map onto a *different*
# routing target than the default. Empty today: the consult set is one "everyday"
# category and all of it is chat-scoped Condor work. The hook stays because it is
# how a future category (a specialist-only consult set, say) would be routed
# without touching the loaders.
CATEGORY_DOMAINS: dict[str, str] = {}

# Tick categories all belong to one domain: they exercise the same agent path.
_TICK_DOMAIN = "tick_execution"
_DEFAULT_CONSULT_DOMAIN = "general_consult"

# Layer 2 domains are namespaced so the router can tell a capability bucket
# ("market_data") from something Condor can actually route ("market_making_expert").
TOOL_DOMAIN_PREFIX = "tool:"

# Expert vs strategy comes from datasets/agent_roles.json, not from code. Users will
# ship their own agents, so this has to be a one-line data decision per agent rather
# than an edit here — and an *unclassified* agent must fail loudly instead of
# defaulting either way, which is what the roster drift test enforces.
AGENT_ROLES_PATH = DATASETS_DIR / "agent_roles.json"


def load_agent_roles() -> dict[str, dict[str, Any]]:
    """slug -> {role, domain?, base?, notes?}. Empty when the file is unreadable."""
    try:
        data = json.loads(AGENT_ROLES_PATH.read_text())
    except Exception:
        return {}
    agents = data.get("agents")
    return agents if isinstance(agents, dict) else {}


def _slugs_with_role(role: str) -> frozenset[str]:
    return frozenset(
        slug
        for slug, spec in load_agent_roles().items()
        if isinstance(spec, dict) and spec.get("role") == role
    )


def strategy_agents() -> frozenset[str]:
    """Agents that specialise another agent, so bench does not route them.

    An XRPL market maker is a market maker pointed at one connector: it calls the
    same tools and inherits its base's model assignment, so giving it a routing
    domain would multiply recommendations on evidence that is the base's evidence
    under a different name.
    """
    return _slugs_with_role("strategy")


def expert_agents() -> frozenset[str]:
    """Agents bench sizes a model for, each getting a routing domain."""
    return _slugs_with_role("expert")


def routing_domain_for(slug: str) -> str:
    """The domain an expert's cases pool into. Usually the slug itself."""
    spec = load_agent_roles().get(slug) or {}
    return str(spec.get("domain") or slug)


def is_routing_domain(domain: str) -> bool:
    """True when a domain names something a Condor model assignment can target.

    False for Layer 2 capability buckets (``tool:market_data`` — there is no config
    key for "market data") and for strategies (see :func:`strategy_agents`).
    """
    if domain.startswith(TOOL_DOMAIN_PREFIX):
        return False
    return domain not in strategy_agents()


def _normalize_risk(value: Any) -> str:
    risk = str(value or "read_only")
    return risk if risk in RISK_LEVELS else "read_only"


@dataclass
class ConsultCase:
    id: str
    question: str
    context: str = ""
    category: str = ""
    expected_tools: list[str] = field(default_factory=list)
    # Additional user messages after the first question (multi-turn)
    turns: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    type: str = "consult"
    # Ground truth for the real-API metrics
    expected_tool_params: dict[str, dict] = field(default_factory=dict)
    live_expected: dict[str, Any] = field(default_factory=dict)
    # Tools that must NOT be called. A consult can be a restraint test — "ask me
    # what you need before building anything" — and without this the ban is
    # unscoreable, because the scorer reads it off the case by name.
    expected_no_calls: list[str] = field(default_factory=list)
    risk_level: str = "read_only"
    agent_slug: str | None = None
    # Ordered build phases. When present, tool accuracy is scored by
    # metrics.tool_accuracy.score_phases instead of multiset F1 — see that
    # function for why a build cannot be scored order-blind.
    steps: list[dict[str, Any]] = field(default_factory=list)
    # Assertions checked against the API *after* the run, before teardown. Same
    # shape as live_expected but the tool is called by bench, not the model:
    # {"manage_routines": {"action": "list", "contains": ["bench_btc_price"]}}
    post_conditions: dict[str, Any] = field(default_factory=dict)

    @property
    def domain(self) -> str:
        return CATEGORY_DOMAINS.get(self.category, _DEFAULT_CONSULT_DOMAIN)


@dataclass
class TickCase:
    id: str
    scenario_name: str
    agent_instructions: str
    strategy_instructions: str
    config: dict[str, Any]
    risk_state: dict[str, Any]
    core_data: dict[str, str]
    learnings: str
    summary: str
    recent_decisions: str
    tick_number: int
    expected_tool_calls: list[str]
    expected_no_calls: list[str] = field(default_factory=list)
    category: str = ""
    tags: list[str] = field(default_factory=list)
    type: str = "tick"
    expected_tool_params: dict[str, dict] = field(default_factory=dict)
    live_expected: dict[str, Any] = field(default_factory=dict)
    risk_level: str = "read_only"
    # Ticks run AS an agent, so they are agent-scoped by construction. Defaults to
    # the case id so a dataset that forgets the field still gets its own store
    # rather than silently borrowing the chat's.
    agent_slug: str | None = None
    # Ordered build phases. When present, tool accuracy is scored by
    # metrics.tool_accuracy.score_phases instead of multiset F1 — see that
    # function for why a build cannot be scored order-blind.
    steps: list[dict[str, Any]] = field(default_factory=list)
    # Assertions checked against the API *after* the run, before teardown. Same
    # shape as live_expected but the tool is called by bench, not the model:
    # {"manage_routines": {"action": "list", "contains": ["bench_btc_price"]}}
    post_conditions: dict[str, Any] = field(default_factory=dict)

    @property
    def domain(self) -> str:
        return _TICK_DOMAIN

    @property
    def expected_tools(self) -> list[str]:
        """Uniform accessor so callers don't branch on case type."""
        return self.expected_tool_calls


@dataclass
class ToolCase:
    """Layer 2: can this model size pick one tool with the right params?"""

    id: str
    tool: str
    question: str
    domain_name: str = ""
    expected_tools: list[str] = field(default_factory=list)
    expected_tool_params: dict[str, dict] = field(default_factory=dict)
    expected_no_calls: list[str] = field(default_factory=list)
    live_expected: dict[str, Any] = field(default_factory=dict)
    risk_level: str = "read_only"
    agent_slug: str | None = None
    tags: list[str] = field(default_factory=list)
    category: str = "tool"
    type: str = "tool"

    @property
    def domain(self) -> str:
        """A ``tool:`` namespace, deliberately not a routing domain.

        Layer 2 groups are capability buckets (market_data, routines, …), not
        things Condor can route to — there is no config key for "market_data". The
        prefix keeps them visible in the matrix while the router skips them, and
        stops a bucket named ``consult`` from colliding with the
        ``general_consult`` routing domain. Per-tool verdicts come out of the
        matrix's ``tools`` axis instead, where one case is a legitimate sample.
        """
        return f"{TOOL_DOMAIN_PREFIX}{self.domain_name or 'other'}"


@dataclass
class AgentCase:
    """Layer 3: a task routed to a specific Condor assistant / agent."""

    id: str
    agent_slug: str | None
    question: str
    assistant: str = ""
    expected_tools: list[str] = field(default_factory=list)
    expected_tool_params: dict[str, dict] = field(default_factory=dict)
    expected_no_calls: list[str] = field(default_factory=list)
    turns: list[str] = field(default_factory=list)
    live_expected: dict[str, Any] = field(default_factory=dict)
    risk_level: str = "read_only"
    tags: list[str] = field(default_factory=list)
    category: str = "agent"
    type: str = "agent"
    # Ordered build phases. When present, tool accuracy is scored by
    # metrics.tool_accuracy.score_phases instead of multiset F1 — see that
    # function for why a build cannot be scored order-blind.
    steps: list[dict[str, Any]] = field(default_factory=list)
    # Assertions checked against the API *after* the run, before teardown. Same
    # shape as live_expected but the tool is called by bench, not the model:
    # {"manage_routines": {"action": "list", "contains": ["bench_btc_price"]}}
    post_conditions: dict[str, Any] = field(default_factory=dict)

    @property
    def domain(self) -> str:
        """The routing target: an agent's slug, or the chat assistant.

        An agent case's domain IS its assistant, because that is the unit a
        recommendation is expressed in ("market_making_expert → qwen2.5:14b"). A
        chat-scoped case (``agent_slug: null``) belongs to ``general_consult``
        alongside the Layer 1 consults rather than to a domain of its own — same
        prompt, same stores, same config key, so splitting them would produce two
        recommendations for one setting.
        """
        return self.agent_slug or _DEFAULT_CONSULT_DOMAIN


Case = ConsultCase | TickCase | ToolCase | AgentCase


def _iter_jsonl(path: Path) -> Iterable[dict]:
    if not path.exists():
        return []
    records = []
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name}:{lineno} is not valid JSON: {exc}") from exc
    return records


def load_consult_cases(path: Path | None = None) -> list[ConsultCase]:
    path = path or DATASETS_DIR / "consult.jsonl"
    return [
        ConsultCase(
            id=data["id"],
            question=data.get("question", ""),
            context=data.get("context", ""),
            category=data.get("category", ""),
            expected_tools=data.get("expected_tools", []),
            turns=data.get("turns", []),
            steps=data.get("steps", []),
            post_conditions=data.get("post_conditions", {}),
            expected_no_calls=data.get("expected_no_calls", []),
            tags=data.get("tags", []),
            type=data.get("type", "consult"),
            expected_tool_params=data.get("expected_tool_params", {}),
            live_expected=data.get("live_expected", {}),
            risk_level=_normalize_risk(data.get("risk_level")),
            agent_slug=data.get("agent_slug"),
        )
        for data in _iter_jsonl(path)
    ]


def load_tick_cases(path: Path | None = None) -> list[TickCase]:
    path = path or DATASETS_DIR / "tick.jsonl"
    cases = []
    for data in _iter_jsonl(path):
        cases.append(
            TickCase(
                id=data["id"],
                scenario_name=data["scenario_name"],
                agent_instructions=data["agent_instructions"],
                strategy_instructions=data["strategy_instructions"],
                config=data.get("config", {}),
                risk_state=data.get("risk_state", {}),
                core_data=data.get("core_data", {}),
                learnings=data.get("learnings", ""),
                summary=data.get("summary", ""),
                recent_decisions=data.get("recent_decisions", ""),
                tick_number=data.get("tick_number", 1),
                expected_tool_calls=data.get("expected_tool_calls", []),
                expected_no_calls=data.get("expected_no_calls", []),
                steps=data.get("steps", []),
                post_conditions=data.get("post_conditions", {}),
                category=data.get("category", ""),
                tags=data.get("tags", []),
                expected_tool_params=data.get("expected_tool_params", {}),
                live_expected=data.get("live_expected", {}),
                risk_level=_normalize_risk(data.get("risk_level")),
                # A tick that omits agent_slug would read the chat's journal and
                # memory instead of the agent's, so fall back to a per-case slug
                # rather than to None.
                agent_slug=data.get("agent_slug") or f"bench_{data['id']}",
            )
        )
    return cases


def load_tool_cases(path: Path | None = None) -> list[ToolCase]:
    path = path or DATASETS_DIR / "tools.jsonl"
    return [
        ToolCase(
            id=data["id"],
            tool=data["tool"],
            question=data.get("question", ""),
            domain_name=data.get("domain", ""),
            # Default the expectation to the tool under test: a per-tool case
            # that names no expected_tools would score every model 1.0.
            expected_tools=data.get("expected_tools") or [data["tool"]],
            expected_tool_params=data.get("expected_tool_params", {}),
            expected_no_calls=data.get("expected_no_calls", []),
            live_expected=data.get("live_expected", {}),
            risk_level=_normalize_risk(data.get("risk_level")),
            agent_slug=data.get("agent_slug"),
            tags=data.get("tags", []),
        )
        for data in _iter_jsonl(path)
    ]


def load_agent_cases(path: Path | None = None) -> list[AgentCase]:
    path = path or DATASETS_DIR / "agents.jsonl"
    return [
        AgentCase(
            id=data["id"],
            # Explicit None (chat-scoped) is a legitimate value, so read the key
            # rather than treating a falsy value as "not set".
            agent_slug=data.get("agent_slug"),
            question=data.get("question", ""),
            assistant=data.get("assistant", ""),
            expected_tools=data.get("expected_tools", []),
            expected_tool_params=data.get("expected_tool_params", {}),
            expected_no_calls=data.get("expected_no_calls", []),
            turns=data.get("turns", []),
            live_expected=data.get("live_expected", {}),
            steps=data.get("steps", []),
            post_conditions=data.get("post_conditions", {}),
            risk_level=_normalize_risk(data.get("risk_level")),
            tags=data.get("tags", []),
        )
        for data in _iter_jsonl(path)
    ]


def load_all_cases(*, layers: Iterable[str] | None = None) -> list[Case]:
    """Load every dataset layer, or just the named ones.

    ``layers`` accepts any of ``consult``, ``tick``, ``tool``, ``agent``.
    """
    wanted = set(layers) if layers else {"consult", "tick", "tool", "agent"}
    cases: list[Case] = []
    if "consult" in wanted:
        cases += load_consult_cases()
    if "tick" in wanted:
        cases += load_tick_cases()
    if "tool" in wanted:
        cases += load_tool_cases()
    if "agent" in wanted:
        cases += load_agent_cases()
    return cases


def case_prompt_map() -> dict[str, str]:
    """Map case_id → user-facing question / scenario name for UI + persistence."""
    prompts: dict[str, str] = {}
    for case in load_all_cases():
        prompts[case.id] = (
            case.scenario_name if case.type == "tick" else getattr(case, "question", "")
        )
    return prompts


def case_domain(case: Case) -> str:
    """Routing domain for a case. Kept as a function so results dicts can reuse it."""
    return case.domain


def is_mutating(case: Case) -> bool:
    return _normalize_risk(getattr(case, "risk_level", None)) != "read_only"


def filter_cases(
    cases: list[Case],
    *,
    domain: str | None = None,
    category: str | None = None,
    layers: Iterable[str] | None = None,
) -> list[Case]:
    """Apply the CLI/dashboard filters in one place."""
    out = list(cases)
    if layers:
        wanted = set(layers)
        out = [c for c in out if c.type in wanted]
    if domain:
        out = [c for c in out if c.domain == domain]
    if category:
        out = [c for c in out if getattr(c, "category", "") == category]
    return out
