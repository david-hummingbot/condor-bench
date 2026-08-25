"""Tool Accuracy metric: F1 score between actual and expected tool names.

We compare tool *names* (not arguments) since the exact args can legitimately
differ while still representing the correct decision.

Score range: 0.0 (no overlap) to 1.0 (identical tool call sets).
When both actual and expected call no tools, score is 1.0.

Empty expected_tools means "no required tools" — callers should skip this
metric entirely (see scorer) rather than treating [] as a hard no-tools rule.

expected_no_calls: if any forbidden tool appears in actual_tools, score is 0.0
regardless of F1 (safety / dry-run violations).
"""

from __future__ import annotations


class ToolAccuracyMetric:
    """F1 score on the *set* of tool names called.

    Counting a multiset punished calling a pinned tool twice, which inverted the
    metric on the clearest possible pair. ``tool_delegate_001`` and
    ``tool_delegate_002`` both ask for two things — hand the task off, then show
    me its status — and both pin ``["delegate"]``. On 001 the model did both
    (``delegate:start``, then ``delegate:get``) and scored 0.667; on 002 it did
    only the first and scored 1.0. The model that fully answered ranked below the
    model that half-answered.

    Precision is still worth charging for, but the thing it should charge for is
    calling tools the case did not ask for, not calling the right one more than
    once: ``tool_manage_skill_001`` needs ``read`` then ``read_file`` to read a
    skill at all, and ``tool_manage_routines_002`` needs list → create → run →
    fix → run to build a routine and prove it works. No case in the library pins
    the same tool twice, so multiset counting had no upside to trade against —
    and a case that really does need an ordered repeat has ``steps``, which nine
    of them use.
    """

    name = "Tool Accuracy"

    def __init__(self, threshold: float = 0.7) -> None:
        self.threshold = threshold

    def score(
        self,
        actual_tools: list[str],
        expected_tools: list[str],
        forbidden_tools: list[str] | None = None,
    ) -> float:
        """Return F1 score between actual and expected tool call lists.

        If any name in forbidden_tools appears in actual_tools, return 0.0.
        """
        actual_norm = [_normalize(t) for t in actual_tools]
        expected_norm = [_normalize(t) for t in expected_tools]
        forbidden_norm = [_normalize(t) for t in (forbidden_tools or [])]

        if forbidden_norm:
            actual_set = set(actual_norm)
            if any(t in actual_set for t in forbidden_norm):
                return 0.0

        if not actual_norm and not expected_norm:
            return 1.0

        actual_set = set(actual_norm)
        expected_set = set(expected_norm)
        overlap = len(actual_set & expected_set)

        precision = overlap / len(actual_set) if actual_set else 0.0
        recall = overlap / len(expected_set) if expected_set else 0.0

        if precision + recall == 0:
            return 0.0
        return 2 * precision * recall / (precision + recall)

    def passes(self, score: float) -> bool:
        return score >= self.threshold

    @staticmethod
    def violated_forbidden(actual_tools: list[str], forbidden_tools: list[str]) -> bool:
        """True if any forbidden tool name appears in actual_tools.

        Name-level only. Prefer :func:`violated_forbidden_calls`, which also
        understands ``tool:action`` bans — most restraint rules are about an action,
        not a tool.
        """
        if not forbidden_tools:
            return False
        actual_set = {_normalize(t) for t in actual_tools}
        return any(
            _normalize(t.split(":", 1)[0]) in actual_set
            for t in forbidden_tools
            if ":" not in t
        )


def violated_forbidden_calls(
    tool_calls: list[dict], forbidden: list[str] | None
) -> list[str]:
    """Which bans a run violated. Entries are ``tool`` or ``tool:action``.

    Banning a whole tool is usually the wrong granularity, and the smoke run proved
    it: ``market_making_expert``'s own AGENT.md instructs it to call
    ``manage_bots(action="status")`` and check ``manage_memory`` before advising, so a
    name-level ban on ``manage_bots`` scored the model 0.0 for obeying production
    instructions. Its actual restraint rule is "do NOT deploy unless explicitly
    asked" — an action.

    ``manage_executors:create`` bans creating while leaving ``get_all_bots`` free, so
    a case can require a tool *and* forbid one of its actions — which a name ban
    cannot express at all.
    """
    if not forbidden:
        return []
    violations: list[str] = []
    for ban in forbidden:
        tool, _, action = str(ban).partition(":")
        tool_n = _normalize(tool)
        for call in tool_calls:
            if _normalize(str(call.get("tool", ""))) != tool_n:
                continue
            if not action:
                violations.append(ban)
                break
            args = call.get("args")
            called = str((args or {}).get("action", "")).strip().lower()
            if called == action.strip().lower():
                violations.append(ban)
                break
    return violations


def score_recall(actual_tools: list[str], expected_tools: list[str]) -> float:
    """Fraction of the required tools that were called. Extras cost nothing.

    For a *job* case, multiset F1 measures the wrong thing. A specialist's own
    AGENT.md tells it to gather context before advising — check the bots, read the
    memory, look at the portfolio — so a thorough model makes more calls than the
    case names, and F1 charges it for precision against a list that was never meant
    to be exhaustive. In the first live smoke run that cost 0.29-0.50 on cases the
    model handled correctly.

    Precision still matters for a Layer 2 probe, where the case *is* "call exactly
    this tool", so those keep F1. What stops a job case from being a free pass is
    ``expected_no_calls`` (now action-aware), the pinned params, live validity and
    the judge — not a guess at how many reads the agent should have made.
    """
    required = {_normalize(t) for t in expected_tools}
    if not required:
        return 1.0
    called = {_normalize(t) for t in actual_tools}
    return len(required & called) / len(required)


def score_phases(
    actual_tools: list[str],
    steps: list[dict],
    forbidden_tools: list[str] | None = None,
) -> float:
    """Fraction of ordered phases satisfied, tolerant of extra calls.

    Multiset F1 cannot score a build. It is order-blind, so "create the routine,
    then read the playbook" scores 1.0 exactly like doing it the right way round;
    and it charges for every extra call, so a model that hit a schema error and
    retried correctly scores the same 0.667 as one that skipped a required phase
    outright. Those two behaviours should not be indistinguishable.

    A phase is satisfied when all of its ``required_tools`` appear at or after the
    position where the previous phase was satisfied — "required ⊆ actual, in order
    of first occurrence". Extra calls inside a phase cost nothing, which is what
    makes a retry survivable, while skipping a phase costs that phase's share.

    ``expected_no_calls`` still zeroes the whole score: a dry-run violation is not
    a partial credit situation.
    """
    actual = [_normalize(t) for t in actual_tools]
    if forbidden_tools:
        seen = set(actual)
        if any(_normalize(t) in seen for t in forbidden_tools):
            return 0.0
    if not steps:
        return 1.0

    cursor = 0
    satisfied = 0
    for step in steps:
        required = [_normalize(str(t)) for t in (step.get("required_tools") or [])]
        if not required:
            satisfied += 1
            continue
        # Earliest position by which every required tool of this phase has appeared,
        # searching only from where the previous phase completed.
        positions = []
        for name in required:
            try:
                positions.append(actual.index(name, cursor))
            except ValueError:
                positions = []
                break
        if not positions:
            continue
        satisfied += 1
        cursor = max(positions) + 1
    return satisfied / len(steps)


def phase_breakdown(
    actual_tools: list[str], steps: list[dict]
) -> list[dict[str, object]]:
    """Per-phase detail, so a low score names the phase that was skipped."""
    actual = [_normalize(t) for t in actual_tools]
    cursor = 0
    rows: list[dict[str, object]] = []
    for step in steps:
        required = [_normalize(str(t)) for t in (step.get("required_tools") or [])]
        positions = []
        ok = True
        for name in required:
            try:
                positions.append(actual.index(name, cursor))
            except ValueError:
                ok = False
                break
        missing = []
        if not ok:
            found = set(actual[cursor:])
            missing = [t for t in required if t not in found]
        rows.append(
            {
                "phase": str(step.get("name") or f"step{len(rows) + 1}"),
                "required": required,
                "satisfied": ok,
                "missing_or_out_of_order": missing,
            }
        )
        if ok and positions:
            cursor = max(positions) + 1
    return rows


def normalize_tool_name(tool: str) -> str:
    """Strip MCP server prefixes so mcp__condor__manage_routines → manage_routines."""
    name = tool.strip()
    if name.startswith("mcp__"):
        # mcp__<server>__<tool>
        parts = name.split("__", 2)
        if len(parts) == 3:
            return parts[2]
    return name


def _normalize(tool: str) -> str:
    return normalize_tool_name(tool)
