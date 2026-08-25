"""Score a benchmark result.

The composite:

    0.45 × quality + 0.20 × tool names + 0.15 × tool params
                   + 0.10 × live validity + 0.10 × latency

Any component with no ground truth for a case scores ``None`` and its weight is
redistributed to answer quality rather than being credited or forfeited. That is
why an advisory consult with no ``expected_tools`` still tops out at 1.0, and why
adding an unpinnable metric can never quietly lower every score.

Token usage is recorded but carries **no weight**: a model shouldn't fail a case
for being token-expensive. Cost is a reporting axis and an opt-in routing
tie-breaker, not a gate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from config import POST_CONDITION_FAIL_CAP, SCORE_WEIGHTS
from bench.client import BenchmarkResult
from bench.post_conditions import post_condition_score
from metrics.answer_quality import AnswerQualityMetric, is_infra_failure
from metrics.judge import JUDGE_USAGE
from metrics.latency import LatencyMetric
from metrics.live_validity import LiveValidityMetric, validity_breakdown
from metrics.tool_accuracy import (
    ToolAccuracyMetric,
    normalize_tool_name,
    phase_breakdown,
    score_phases,
    score_recall,
    violated_forbidden_calls,
)
from metrics.tool_params import ToolParamMetric, param_breakdown

_quality_metric = AnswerQualityMetric()
_tool_metric = ToolAccuracyMetric()
_param_metric = ToolParamMetric()
_validity_metric = LiveValidityMetric()
_latency_metric = LatencyMetric()


def normalize_expected_tools(expected_tools: list[str] | None) -> list[str] | None:
    """Empty list → None (advisory: no tool ground truth)."""
    if expected_tools is None:
        return None
    if len(expected_tools) == 0:
        return None
    return expected_tools


@dataclass
class ScoreCard:
    case_id: str
    model: str
    # None when no judgement could be made — an infra failure or a judge that did not
    # answer. Distinct from 0.0, which asserts the model gave a bad answer.
    answer_quality: float | None
    answer_reason: str
    tool_accuracy: float | None  # None when no required expected_tools
    latency_score: float
    composite: float
    latency_s: float
    baseline_latency_s: float
    error: str | None = None
    category: str = ""
    # Dataset layer this case came from (consult | tick | tool | agent). Persisted
    # because the dashboard cannot infer it: chat-scoped Layer 3 cases were merged
    # into the consult layer but kept their `agent_*` ids, so an id-prefix guess
    # labels eight of them as the wrong layer.
    case_type: str = ""
    tool_calls: list[str] = field(default_factory=list)
    # The tools this case is evidence about. Persisted so the per-tool matrix can
    # be rebuilt from results alone, without re-reading (a possibly since-edited)
    # dataset.
    expected_tools: list[str] = field(default_factory=list)
    # None when the case pins nothing to score them against.
    tool_params: float | None = None
    live_validity: float | None = None
    # Context for the matrix and the router
    domain: str = ""
    risk_level: str = "read_only"
    usage: dict[str, Any] = field(default_factory=dict)
    judge_usage: dict[str, Any] = field(default_factory=dict)
    wiring: dict[str, Any] = field(default_factory=dict)
    # Full traces + per-key detail, for debugging a score rather than guessing
    tool_call_details: list[dict[str, Any]] = field(default_factory=list)
    tool_param_detail: dict[str, Any] = field(default_factory=dict)
    live_validity_detail: dict[str, Any] = field(default_factory=dict)
    # Per-phase results for cases that declare ordered steps; empty otherwise.
    phase_detail: list[dict[str, Any]] = field(default_factory=list)
    # Set when the harness — not the model — is why this row is bad. Excluded
    # from routing so a misconfiguration can't become a model recommendation.
    harness_artifact: str | None = None
    # Set when the measurement is valid but carries a caveat — something worth
    # knowing when comparing runs, not a reason to throw the row away. Kept
    # separate from `harness_artifact` because exclusion is total: a full ACP run
    # flagged all 80 cases for having one extra unused tool available, so
    # `cases_scored` was 0, `composite_avg` 0.0, and the matrix had nothing to
    # route from. A caveat is reported and still scored.
    harness_note: str | None = None
    # Bans the run violated, as `tool` or `tool:action`. Empty on a clean run — a
    # tool_accuracy of 0.0 with no explanation is exactly the debugging dead end the
    # first smoke run hit.
    forbidden_violations: list[str] = field(default_factory=list)
    # The agent's own tools it reached for (Claude Code's ToolSearch, Read, …).
    # Reported, never scored: they are not decisions about condor's surface. Kept
    # visible so "the tool score ignored these" is a statement you can check rather
    # than trust.
    agent_internal_calls: list[str] = field(default_factory=list)
    # Set when a post-condition probe ran and the asserted end state was not there.
    # Unlike harness_artifact this *is* the model's failure: the composite is capped
    # so the case cannot pass.
    post_condition_failed: str | None = None
    # Expected MCP tools the agent appears to have served with one of its own
    # built-ins instead — `[{"expected": "manage_memory", "native": ["Write"]}]`.
    # Diagnostic only: the score is unchanged, because routing around the tool is
    # a real outcome and sometimes a failed one. What this fixes is the *reading*.
    # See :func:`_detect_tool_substitutions`.
    tool_substitutions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        from config import PASS_THRESHOLD

        return self.error is None and self.composite >= PASS_THRESHOLD

    def as_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "model": self.model,
            "category": self.category,
            "case_type": self.case_type,
            "domain": self.domain,
            "risk_level": self.risk_level,
            "answer_quality": (
                round(self.answer_quality, 4) if self.answer_quality is not None else None
            ),
            "answer_reason": self.answer_reason,
            "tool_accuracy": round(self.tool_accuracy, 4) if self.tool_accuracy is not None else None,
            "tool_params": round(self.tool_params, 4) if self.tool_params is not None else None,
            "live_validity": round(self.live_validity, 4) if self.live_validity is not None else None,
            "latency_score": round(self.latency_score, 4),
            "composite": round(self.composite, 4),
            "latency_s": round(self.latency_s, 2),
            "baseline_latency_s": round(self.baseline_latency_s, 2),
            "error": self.error,
            "harness_artifact": self.harness_artifact,
            "harness_note": self.harness_note,
            "post_condition_failed": self.post_condition_failed,
            "tool_substitutions": self.tool_substitutions,
            "forbidden_violations": self.forbidden_violations,
            "agent_internal_calls": self.agent_internal_calls,
            "tool_calls": self.tool_calls,
            "expected_tools": self.expected_tools,
            "tool_call_details": self.tool_call_details,
            "tool_param_detail": self.tool_param_detail,
            "live_validity_detail": self.live_validity_detail,
            "phase_detail": self.phase_detail,
            "usage": self.usage,
            "judge_usage": self.judge_usage,
            "wiring": self.wiring,
        }


def _composite(
    components: dict[str, float | None], weights: dict[str, float] = SCORE_WEIGHTS
) -> float:
    """Weighted mean over the scorable components, quality absorbing the rest.

    A component is unscorable when it is ``None`` (no ground truth) or carries
    zero weight. Its weight moves to answer quality, so the sum of applied
    weights is always 1.0 and composites stay comparable across cases with
    different amounts of ground truth.
    """
    quality = components.get("answer_quality")
    total = 0.0
    absorbed = weights.get("answer_quality", 0.0)
    scorable: list[tuple[str, float, float]] = []
    for name, weight in weights.items():
        if name == "answer_quality":
            continue
        value = components.get(name)
        if value is None or weight <= 0:
            absorbed += weight
            continue
        total += weight * value
        scorable.append((name, weight, value))

    if quality is not None:
        return total + absorbed * quality

    # Quality is the absorber every other unscorable component folds into, so a None
    # here cannot fall through to 0.0 — that would keep the absorbed weight in the
    # denominator and score it zero, capping a flawless case at 0.55. Renormalise over
    # whatever *was* measurable instead: "we could not judge the prose, so this is the
    # score on the parts we could check."
    measured_weight = sum(w for _, w, _ in scorable)
    if not measured_weight:
        # Nothing at all was scorable. 0.0 would read as a model failure; the caller
        # marks these rows so the matrix excludes them.
        return 0.0
    return total / measured_weight


async def score(
    result: BenchmarkResult,
    input_text: str,
    expected_tools: list[str] | None,
    baseline_latency_s: float,
    *,
    expected_no_calls: list[str] | None = None,
    expected_tool_params: dict[str, dict] | None = None,
    live_expected: dict[str, Any] | None = None,
    steps: list[dict] | None = None,
    strict_tools: bool = False,
    domain: str = "",
    risk_level: str = "read_only",
) -> ScoreCard:
    """Score a result against dataset ground truth (no baseline response needed).

    expected_no_calls: tools that must NOT appear; any hit → tool_accuracy 0.
    """
    # Scored against condor's MCP surface only. An ACP agent's own built-ins
    # (Claude Code's ToolSearch, Read, …) are in the trace but are not decisions
    # about condor: counting them cost Layer 2 F1 precision no model could recover
    # and diluted live validity, which is a claim about the real API. The full trace
    # is still persisted and displayed — see BenchmarkResult's scoring views.
    tool_names = result.mcp_tool_names()
    scored_calls = result.mcp_tool_calls
    scored_responses = result.mcp_tool_responses
    internal_calls = result.agent_internal_tool_names()
    judge_before = JUDGE_USAGE.snapshot()

    harness_artifact = _detect_harness_artifact(result, expected_tools)
    harness_note = _detect_harness_note(result)

    def _card(**overrides: Any) -> ScoreCard:
        base: dict[str, Any] = {
            "case_id": result.case_id,
            "model": result.model,
            "domain": domain,
            "risk_level": risk_level,
            "usage": dict(result.usage),
            "wiring": dict(result.wiring),
            # `tool_calls` is the scored list (MCP surface only); the untouched trace
            # lives on `tool_call_details`, and what was set aside on
            # `agent_internal_calls`.
            "tool_calls": tool_names,
            "expected_tools": list(expected_tools or []),
            "tool_call_details": result.tool_calls,
            "agent_internal_calls": internal_calls,
            "baseline_latency_s": baseline_latency_s,
            "latency_s": result.latency_s,
            "harness_artifact": harness_artifact,
            "harness_note": harness_note,
        }
        base.update(overrides)
        return ScoreCard(**base)

    # Provider/infra failures — mark error so summary averages exclude them
    infra_blob = result.response or (result.error or "")
    if is_infra_failure(infra_blob):
        raw = infra_blob.strip() or "empty infra failure"
        return _card(
            answer_quality=0.0,
            answer_reason=f"Infrastructure error: {raw[:160]}",
            tool_accuracy=None,
            latency_score=0.0,
            composite=0.0,
            error=f"infra: {raw[:200]}",
        )

    # `response=` is the gate input; the transcript is what the judge reads. Handing
    # the transcript to the infra check let a skill file that mentions "rate limit"
    # register as a provider outage.
    answer_quality, answer_reason = await _quality_metric.a_score(
        input_text, result.transcript_for_judge(), response=result.response
    )

    required = normalize_expected_tools(expected_tools)
    forbidden = list(expected_no_calls or [])
    # Bans are checked against the full calls, not just names, so `tool:action`
    # entries work — a case can require manage_executors and still forbid
    # manage_executors:create.
    violations = violated_forbidden_calls(scored_calls, forbidden)

    tool_accuracy: float | None = None
    phase_detail: list[dict[str, Any]] = []
    if violations:
        # A restraint violation is not a partial-credit situation.
        tool_accuracy = 0.0
    elif steps:
        # Ordered phases instead of F1: F1 is order-blind (building before reading
        # the playbook scores full marks) and charges for retries (recovering from a
        # schema error looks the same as skipping a phase).
        tool_accuracy = score_phases(tool_names, steps)
        phase_detail = phase_breakdown(tool_names, steps)
    elif required is not None:
        # Layer 2 probes are "call exactly this tool", so precision counts. Job cases
        # are scored on recall: the agent's own prompt tells it to gather context, and
        # charging it for calls the case did not happen to list measures nothing.
        tool_accuracy = (
            _tool_metric.score(actual_tools=tool_names, expected_tools=required)
            if strict_tools
            else score_recall(tool_names, required)
        )
    elif forbidden:
        tool_accuracy = 1.0

    params = expected_tool_params or {}
    tool_params = _param_metric.score(scored_calls, params)
    param_detail = param_breakdown(scored_calls, params) if params else {}

    live_validity = _validity_metric.score(
        scored_responses, live_expected, expected_tools=required
    )
    validity_detail = validity_breakdown(scored_responses, live_expected)

    # Post-conditions belong to live validity, not to a sixth weighted metric:
    # both answer "did this actually work against the real API", one from the
    # response the model saw and one from the state it left behind. Folding them
    # keeps SCORE_WEIGHTS summing to 1.0 and needs no reweighting.
    probe_score = post_condition_score(result.post_conditions)
    post_condition_failed: str | None = None
    if probe_score is not None:
        live_validity = (
            probe_score if live_validity is None else (live_validity + probe_score) / 2
        )
        # Anything short of 1.0, not just 0.0. "The routine does not exist" scores
        # 0.5 against {action: list, contains: [name]} — `nonempty` passes on a list
        # holding *other* routines — so a ==0 test would miss the real failure mode.
        # A post-condition is a binary claim about end state; partial credit on one
        # means the state is not what the case asserted.
        if probe_score < 1.0:
            unmet = sorted(
                str(row.get("tool"))
                for row in result.post_conditions
                if row.get("score") is not None and row["score"] < 1.0
            )
            post_condition_failed = (
                f"post-condition not met for {', '.join(unmet)} — the case asserted "
                "an end state that was not there afterwards"
            )
    validity_detail = dict(validity_detail or {})
    if result.post_conditions:
        validity_detail["post_conditions"] = result.post_conditions

    latency_score = _latency_metric.score(
        test_latency=result.latency_s,
        baseline_latency=baseline_latency_s,
    )

    composite = _composite(
        {
            "answer_quality": answer_quality,
            "tool_accuracy": tool_accuracy,
            "tool_params": tool_params,
            "live_validity": live_validity,
            "latency_score": latency_score,
        }
    )

    if post_condition_failed:
        composite = min(composite, POST_CONDITION_FAIL_CAP)

    # An unjudgeable answer is not automatically an unusable row, and the two cases
    # differ. An *infra* failure means the model never really answered, so the row is
    # marked and the matrix drops it — the reason string used to promise that
    # ("excluded from model-quality avg") while nothing set `error`, so a 0.0 sailed
    # into the averages. A *judge* failure means the model answered and the judge did
    # not: the tool evidence is still good, so the row stays and the composite is
    # renormalised over what was measurable.
    quality_infra = answer_quality is None and answer_reason.startswith(
        "Infrastructure error"
    )

    return _card(
        answer_quality=answer_quality,
        answer_reason=answer_reason,
        post_condition_failed=post_condition_failed,
        tool_substitutions=_detect_tool_substitutions(
            required, tool_names, internal_calls
        ),
        forbidden_violations=violations,
        tool_accuracy=tool_accuracy,
        phase_detail=phase_detail,
        tool_params=tool_params,
        live_validity=live_validity,
        latency_score=latency_score,
        composite=composite,
        error=result.error or (f"infra: {answer_reason[:200]}" if quality_infra else None),
        judge_usage=JUDGE_USAGE.delta_since(judge_before),
        tool_param_detail=param_detail,
        live_validity_detail=validity_detail,
    )


def timeout_card(case: Any, model: str, timeout_s: float, baseline_latency_s: float = 0.0):
    """Scorecard for a case killed by the wall-clock ceiling.

    A timeout used to append *nothing* in the CLI path, so the case simply vanished:
    two `solana_dex_lp_expert` cases disappeared from a run and took 40% of that
    domain's evidence with them, with no trace in the summary. Silence is the one thing
    a benchmark must not do with a case it failed to measure — thin evidence has to look
    thin.

    Marked `harness_artifact` rather than scored 0.0, which is the same treatment market
    warmup failures get (see :func:`bench.market_warmup.warmup_failure_card`): the model
    was never measured, so the row is excluded from the matrix instead of being averaged
    in as a bad answer.
    """
    return ScoreCard(
        case_id=getattr(case, "id", "?"),
        model=model,
        category=getattr(case, "category", "") or "",
        case_type=getattr(case, "type", "") or "",
        domain=getattr(case, "domain", "") or "",
        risk_level=getattr(case, "risk_level", "read_only") or "read_only",
        answer_quality=None,
        answer_reason=f"timed out after {timeout_s:.0f}s",
        tool_accuracy=None,
        tool_params=None,
        live_validity=None,
        latency_score=0.0,
        composite=0.0,
        latency_s=timeout_s,
        baseline_latency_s=baseline_latency_s,
        expected_tools=list(getattr(case, "expected_tools", []) or []),
        harness_artifact=(
            f"case exceeded its {timeout_s:.0f}s ceiling "
            f"(baseline {baseline_latency_s:.1f}s) — the model was not measured, so this "
            "row is excluded rather than scored"
        ),
        error=f"timeout after {timeout_s:.0f}s",
    )


# Which of the agent's own built-ins can stand in for which condor tool. Keyed by
# the MCP tool the case asked for; the values are the built-ins that reach the same
# state by another road. Only names that genuinely overlap belong here — this drives
# a diagnostic, so a loose entry produces a misleading one.
_NATIVE_SUBSTITUTES: dict[str, tuple[str, ...]] = {
    "manage_skill": ("Read", "Bash", "Glob", "Grep", "Skill"),
    "manage_memory": ("Write", "Edit", "Read", "Bash"),
    "manage_notes": ("Write", "Edit", "Read", "Bash"),
    "configure_server": ("Bash", "Read"),
    "manage_servers": ("Bash", "Read"),
    "get_available_models": ("Skill", "Bash", "WebFetch"),
    "get_user_context": ("Bash", "Read"),
    "run_code": ("Bash",),
}


def _detect_tool_substitutions(
    required: list[str] | None,
    called: list[str],
    internal_calls: list[str],
) -> list[dict[str, Any]]:
    """Expected MCP tools the agent served with one of its own built-ins instead.

    Six rows of the 80-case claude-code run read as capability gaps and were not.
    `agent_directional_trader_002` and `_003` were told to read a skill and read the
    files off disk with `Read`; `_008` was told to remember a rule and wrote a file
    with `Write`; `tool_configure_server_003` answered "which username" by `cat`-ing
    config.yml; `tool_get_available_models_003` answered from a `Skill` instead of
    asking the tool. Each scored tool_accuracy 0.0 while the judge scored the prose
    0.82-0.90, and directional_trader came out the weakest domain in the run at 0.40
    pass — on three failures that were all this.

    The score deliberately does not move. Routing around the tool is a real outcome
    and sometimes a failing one: `_008`'s post-condition probe went looking for the
    memory in condor and did not find it, because a local file is not a condor
    memory and the next tick will not see it. What was wrong was only the *reading* —
    nothing in the row said "the model reached past the tool", so the number looked
    like an inability to do the job. This says it.
    """
    if not required:
        return []
    called_set = {normalize_tool_name(t) for t in called}
    internal_set = set(internal_calls)
    rows: list[dict[str, Any]] = []
    for tool in required:
        name = normalize_tool_name(tool)
        if name in called_set:
            continue
        used = [n for n in _NATIVE_SUBSTITUTES.get(name, ()) if n in internal_set]
        if used:
            rows.append({"expected": name, "native": sorted(used)})
    return rows


def _detect_harness_note(result: BenchmarkResult) -> str | None:
    """A caveat worth recording that does not invalidate the measurement.

    The distinction this exists to make: an ACP run auto-discovers whatever stdio
    servers ``condor/.mcp.json`` declares, so every case gets ``playwright``
    offered alongside the 24 condor/hummingbot tools. bench cannot suppress it
    from its side (see ``bench.mcp_provider.wiring_metadata``), and the tool count
    genuinely differs from the PydanticAI path — so a cross-path comparison of
    tool scores needs to know. But the model still ran against the right target
    with the right tools, and no case needs a browser.

    Treating that as a harness *artifact* excluded every row: a full 80-case
    claude-code run reported ``cases_scored: 0``, ``composite_avg: 0.0`` and an
    empty matrix, so the run could not produce a routing recommendation at all.

    Merely being *offered* an extra server is not worth a note either. The
    80-case claude-code run stamped this caveat on all 79 scored rows for a
    playwright server that was invoked exactly zero times — flagging every tool
    score in the run as cross-transport-incomparable on account of a tool no
    case touched. What makes a row genuinely incomparable is an extra server the
    model actually *called*, so that is what this looks for.
    """
    wiring = result.wiring or {}
    extras = [str(e) for e in (wiring.get("autodiscovery_extras") or [])]
    if not extras:
        return None
    called = set(result.tool_names())
    used = [e for e in extras if any(t.startswith(f"mcp__{e}__") for t in called)]
    if not used:
        return None
    names = ", ".join(used)
    return (
        f"the model called {names}, auto-discovered from condor/.mcp.json — that "
        "server is not on the PydanticAI path, so tool scores for this case are "
        "not directly comparable across transports"
    )


def _detect_harness_artifact(
    result: BenchmarkResult, expected_tools: list[str] | None = None
) -> str | None:
    """Name the harness misconfiguration behind a bad row, if that's what it is.

    The failure this guards against: an agent-scoped case that ran chat-scoped
    reads the wrong condor stores, fails, and gets averaged in as evidence the
    model is too small. Flagged rows are excluded from the routing matrix, the
    same treatment infra failures already get.
    """
    wiring = result.wiring or {}

    # A Layer 3 case measured against the generic Condor prompt instead of its
    # own assistant's is not a test of that assistant.
    #
    # Bench's own probe agents are the exception: `bench_journal_probe` is a
    # journal fixture, not an assistant, and it has no AGENT.md by design — the
    # generic Condor prompt is the *correct* prompt for it. Flagging it excluded
    # all four journal cases from every run for having no instructions they were
    # never supposed to have.
    prompt_source = wiring.get("assistant_prompt")
    slug = str(wiring.get("agent_slug") or "")
    if (
        isinstance(prompt_source, str)
        and prompt_source.startswith("fallback:")
        and not slug.startswith("bench_")
    ):
        return (
            f"assistant prompt fell back ({prompt_source}) — the model was given "
            "the generic Condor prompt, not this agent's instructions"
        )

    if not wiring.get("api_url"):
        return "run resolved no API URL — MCP wiring did not report a target"

    from metrics.tool_accuracy import normalize_tool_name

    # A journal the harness never provisioned is not a model failure. condor resolves
    # a journal from `{agent_slug}.{strategy_slug}_{n}` to a directory on disk, so a
    # probe agent that does not exist there answers "no journal available for this
    # agent" no matter what the model did. Four tick cases pin
    # `trading_agent_journal_write` as an expected call, and every one of them scored
    # live_validity 0.0 on that — then lost answer_quality again for "silently ignoring
    # the failed journal write". Both deductions were the fixture's absence.
    for record in getattr(result, "tool_responses", []) or []:
        tool = normalize_tool_name(str(record.get("tool", "")))
        if not tool.startswith("trading_agent_journal"):
            continue
        text = str(record.get("output") or "")
        if "no journal available for this agent" in text.lower():
            return (
                "the journal for this case's probe agent does not exist on the condor "
                "checkout, so the write could not succeed however the model behaved — "
                "provision agents/<slug>/strategies/<case_id>/sessions/session_1"
            )

    # A case cannot fail on a tool it was never shown. The model-size cap trims
    # `tool_defs[:limit]`, so a case whose expected tool sorted past the cut is
    # measuring the harness, not the model — the failure mode that made scoping
    # specialists to their grant worth doing in the first place.
    offered = wiring.get("offered_tools")
    if isinstance(offered, list) and offered and expected_tools:
        have = {normalize_tool_name(str(t)) for t in offered}
        missing = sorted(
            {normalize_tool_name(str(t)) for t in expected_tools} - have
        )
        if missing:
            return (
                f"expected tool(s) {', '.join(missing)} were never offered to the "
                f"model (it saw {len(have)}: {', '.join(sorted(have))}) — the case "
                "measures the tool filter, not the model"
            )
    return None


async def score_case(
    case: Any,
    result: BenchmarkResult,
    baseline_latency_s: float,
) -> ScoreCard:
    """Score a result using the case's own ground-truth fields.

    Single place where dataset field names map onto scorer arguments, so the CLI,
    the dashboard and the sweep runner cannot disagree about (for example) whether
    tick cases pass ``expected_no_calls``.
    """
    from bench.client import case_input_text

    card = await score(
        result,
        case_input_text(case),
        list(getattr(case, "expected_tools", []) or []),
        baseline_latency_s,
        expected_no_calls=list(getattr(case, "expected_no_calls", []) or []) or None,
        expected_tool_params=getattr(case, "expected_tool_params", {}) or None,
        live_expected=getattr(case, "live_expected", {}) or None,
        steps=list(getattr(case, "steps", []) or []) or None,
        # Layer 2 probes are the only cases where tool *precision* is the point.
        strict_tools=getattr(case, "type", "") == "tool",
        domain=getattr(case, "domain", ""),
        risk_level=getattr(case, "risk_level", "read_only"),
    )
    card.category = getattr(case, "category", "")
    card.case_type = getattr(case, "type", "")
    return card
