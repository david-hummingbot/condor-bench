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
    answer_quality: float
    answer_reason: str
    tool_accuracy: float | None  # None when no required expected_tools
    latency_score: float
    composite: float
    latency_s: float
    baseline_latency_s: float
    error: str | None = None
    category: str = ""
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
    # Bans the run violated, as `tool` or `tool:action`. Empty on a clean run — a
    # tool_accuracy of 0.0 with no explanation is exactly the debugging dead end the
    # first smoke run hit.
    forbidden_violations: list[str] = field(default_factory=list)
    # Set when a post-condition probe ran and the asserted end state was not there.
    # Unlike harness_artifact this *is* the model's failure: the composite is capped
    # so the case cannot pass.
    post_condition_failed: str | None = None

    @property
    def passed(self) -> bool:
        from config import PASS_THRESHOLD

        return self.error is None and self.composite >= PASS_THRESHOLD

    def as_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "model": self.model,
            "category": self.category,
            "domain": self.domain,
            "risk_level": self.risk_level,
            "answer_quality": round(self.answer_quality, 4),
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
            "post_condition_failed": self.post_condition_failed,
            "forbidden_violations": self.forbidden_violations,
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
    quality = components.get("answer_quality") or 0.0
    total = 0.0
    absorbed = weights.get("answer_quality", 0.0)
    for name, weight in weights.items():
        if name == "answer_quality":
            continue
        value = components.get(name)
        if value is None or weight <= 0:
            absorbed += weight
            continue
        total += weight * value
    return total + absorbed * quality


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
    tool_names = result.tool_names()
    judge_before = JUDGE_USAGE.snapshot()

    harness_artifact = _detect_harness_artifact(result, expected_tools)

    def _card(**overrides: Any) -> ScoreCard:
        base: dict[str, Any] = {
            "case_id": result.case_id,
            "model": result.model,
            "domain": domain,
            "risk_level": risk_level,
            "usage": dict(result.usage),
            "wiring": dict(result.wiring),
            "tool_calls": tool_names,
            "expected_tools": list(expected_tools or []),
            "tool_call_details": result.tool_calls,
            "baseline_latency_s": baseline_latency_s,
            "latency_s": result.latency_s,
            "harness_artifact": harness_artifact,
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

    answer_quality, answer_reason = await _quality_metric.a_score(
        input_text, result.transcript_for_judge()
    )

    required = normalize_expected_tools(expected_tools)
    forbidden = list(expected_no_calls or [])
    # Bans are checked against the full calls, not just names, so `tool:action`
    # entries work — a case can require manage_executors and still forbid
    # manage_executors:create.
    violations = violated_forbidden_calls(result.tool_calls, forbidden)

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
    tool_params = _param_metric.score(result.tool_calls, params)
    param_detail = param_breakdown(result.tool_calls, params) if params else {}

    live_validity = _validity_metric.score(
        result.tool_responses, live_expected, expected_tools=required
    )
    validity_detail = validity_breakdown(result.tool_responses, live_expected)

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

    return _card(
        answer_quality=answer_quality,
        answer_reason=answer_reason,
        post_condition_failed=post_condition_failed,
        forbidden_violations=violations,
        tool_accuracy=tool_accuracy,
        phase_detail=phase_detail,
        tool_params=tool_params,
        live_validity=live_validity,
        latency_score=latency_score,
        composite=composite,
        error=result.error,
        judge_usage=JUDGE_USAGE.delta_since(judge_before),
        tool_param_detail=param_detail,
        live_validity_detail=validity_detail,
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
    prompt_source = wiring.get("assistant_prompt")
    if isinstance(prompt_source, str) and prompt_source.startswith("fallback:"):
        return (
            f"assistant prompt fell back ({prompt_source}) — the model was given "
            "the generic Condor prompt, not this agent's instructions"
        )

    if not wiring.get("api_url"):
        return "run resolved no API URL — MCP wiring did not report a target"
    if wiring.get("autodiscovery_extras"):
        extras = ", ".join(str(e) for e in wiring["autodiscovery_extras"])
        return (
            f"ACP auto-discovery added {extras} from condor/.mcp.json — the tool "
            "set differs from the PydanticAI path, so tool scores are not comparable"
        )

    # A case cannot fail on a tool it was never shown. The model-size cap trims
    # `tool_defs[:limit]`, so a case whose expected tool sorted past the cut is
    # measuring the harness, not the model — the failure mode that made scoping
    # specialists to their grant worth doing in the first place.
    offered = wiring.get("offered_tools")
    if isinstance(offered, list) and offered and expected_tools:
        from metrics.tool_accuracy import normalize_tool_name

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
    return card
