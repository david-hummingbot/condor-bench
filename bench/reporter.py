"""Generate and persist benchmark run reports."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import DOMAIN_PASS_RATE, PASS_THRESHOLD, RESULTS_DIR
from bench import stats
from bench.scorer import ScoreCard

log = logging.getLogger(__name__)


def trial_key(case_id: str, trial_idx: int = 1) -> str:
    """Key for one trial's case file and response.

    Trial 1 keeps the bare case id, so single-trial runs — and every run saved
    before trials existed — keep ``cases/<case_id>.json``.
    """
    return case_id if int(trial_idx or 1) <= 1 else f"{case_id}__t{int(trial_idx)}"


def save_run(
    model: str,
    scorecards: list[ScoreCard],
    responses: dict[str, str],
    run_id: str | None = None,
    prompts: dict[str, str] | None = None,
    *,
    extra_summary: dict[str, Any] | None = None,
) -> Path:
    """Persist a full benchmark run to results/{run_id}_{model}/."""
    run_id = run_id or _utc_stamp()
    safe_model = model.replace(":", "_").replace("/", "_")
    run_dir = RESULTS_DIR / f"{run_id}_{safe_model}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Cases first. They are the irreplaceable half — a summary can be recomputed
    # from them at any time, while re-running the suite costs the better part of an
    # hour and cannot reproduce the same responses. Writing the summary first meant
    # one arithmetic error in the headline discarded every case file behind it.
    cases_dir = run_dir / "cases"
    cases_dir.mkdir(exist_ok=True)
    for sc in scorecards:
        key = trial_key(sc.case_id, sc.trial_idx)
        record = sc.as_dict()
        record["response"] = responses.get(key, "")
        if prompts and sc.case_id in prompts:
            record["question"] = prompts[sc.case_id]
        (cases_dir / f"{key}.json").write_text(json.dumps(record, indent=2))

    try:
        summary = _compute_summary(model, scorecards)
    except Exception as exc:
        # Never trade the run for its headline. The cases are already on disk, so
        # record what broke and leave the run loadable rather than raising through
        # a completed run and leaving a directory that looks like a crash.
        log.exception("summary computation failed for run %s", run_dir.name)
        summary = {
            "model": model,
            "cases_total": len(scorecards),
            "summary_error": f"{type(exc).__name__}: {exc}",
        }
    if extra_summary:
        summary.update(extra_summary)
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    return run_dir


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile. None for an empty series."""
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[idx]


def _usage_totals(scorecards: list[ScoreCard]) -> dict[str, Any]:
    """Token/cost roll-up over the cases that reported usage.

    Cases with no usage are excluded from the averages rather than counted as
    zero: not every backend reports tokens, and averaging silence as free would
    make an unmeasured local model look like the cheapest option in the matrix.
    """
    rows = [sc.usage for sc in scorecards if sc.usage]
    totals: dict[str, Any] = {"cases_with_usage": len(rows)}
    if not rows:
        return totals

    for field_name in ("input_tokens", "output_tokens", "total_tokens"):
        values = [float(r[field_name]) for r in rows if r.get(field_name) is not None]
        if values:
            totals[f"{field_name}_sum"] = int(sum(values))
            totals[f"avg_{field_name}"] = round(_mean(values), 1)

    p95 = _percentile(
        [float(r["total_tokens"]) for r in rows if r.get("total_tokens") is not None], 95
    )
    if p95 is not None:
        totals["p95_total_tokens"] = int(p95)

    costs = [float(r["cost_usd"]) for r in rows if r.get("cost_usd") is not None]
    if costs:
        totals["cost_usd_sum"] = round(sum(costs), 6)
        totals["avg_cost_usd"] = round(_mean(costs), 6)
        totals["cases_priced"] = len(costs)
    else:
        # An unpriced model (Ollama, a custom endpoint) is distinct from a free
        # one; say so rather than reporting 0.
        totals["avg_cost_usd"] = None
        totals["cases_priced"] = 0

    return totals


def _judge_totals(scorecards: list[ScoreCard]) -> dict[str, Any]:
    rows = [sc.judge_usage for sc in scorecards if sc.judge_usage]
    if not rows:
        return {}
    out: dict[str, Any] = {
        "cases_judged": len(rows),
        "input_tokens_sum": int(sum(r.get("input_tokens", 0) for r in rows)),
        "output_tokens_sum": int(sum(r.get("output_tokens", 0) for r in rows)),
    }
    out["total_tokens_sum"] = out["input_tokens_sum"] + out["output_tokens_sum"]
    costs = [float(r["cost_usd"]) for r in rows if r.get("cost_usd") is not None]
    if costs:
        out["cost_usd_sum"] = round(sum(costs), 6)
    return out


def _domain_breakdown(scorecards: list[ScoreCard]) -> dict[str, Any]:
    """Per-domain pass rate and mean composite — the matrix's per-run input."""
    by_domain: dict[str, list[ScoreCard]] = {}
    for sc in scorecards:
        by_domain.setdefault(sc.domain or "unclassified", []).append(sc)

    out: dict[str, Any] = {}
    for domain, cards in sorted(by_domain.items()):
        scorable = [c for c in cards if c.error is None and not c.harness_artifact]
        if not scorable:
            out[domain] = {
                "cases": len(cards),
                "scored": 0,
                "pass_rate": None,
                "avg_composite": None,
                "excluded": len(cards),
            }
            continue
        passed = [c for c in scorable if c.passed]
        rate = stats.rate_summary(len(passed), len(scorable), gate=DOMAIN_PASS_RATE)
        out[domain] = {
            "cases": len(cards),
            "scored": len(scorable),
            "excluded": len(cards) - len(scorable),
            "pass_rate": round(len(passed) / len(scorable), 4),
            "pass_rate_ci": rate["ci"],
            "gate_status": rate["gate_status"],
            "avg_composite": round(_mean([c.composite for c in scorable]), 4),
        }
    return out


def _group_rates(valid: list[ScoreCard], attr: str) -> dict[str, Any]:
    """Pass rate with a Wilson interval per task category or split."""
    groups: dict[str, list[ScoreCard]] = {}
    for s in valid:
        key = getattr(s, attr, "") or ""
        if key:
            groups.setdefault(key, []).append(s)
    out: dict[str, Any] = {}
    for key, cards in sorted(groups.items()):
        passes = sum(1 for c in cards if c.passed)
        rate = stats.rate_summary(passes, len(cards))
        out[key] = {"scored": len(cards), "passed": passes, "pass_rate": rate["rate"], "pass_rate_ci": rate["ci"]}
    return out


def _is_unsafe(card: ScoreCard) -> bool:
    return bool(card.forbidden_violations) or card.unsafe_action_rate > 0.0


def _safety_rows(scorecards: list[ScoreCard], valid: list[ScoreCard]) -> list[ScoreCard]:
    """The rows the unsafe rate is computed over: every scored row, plus every
    excluded row that made a forbidden call.

    Exclusion says the harness spoiled the *score*; it cannot un-place an order. A
    provider that dropped mid-trial after the agent called a banned action left an
    infra row, and counting only `valid` rows erased that action from the unsafe
    rate — the same laundering `bench.trials.flag_mock_gaps` refuses to do.
    """
    kept = {id(sc) for sc in valid}
    return valid + [sc for sc in scorecards if id(sc) not in kept and _is_unsafe(sc)]


def _trial_stats(valid: list[ScoreCard], safety_rows: list[ScoreCard] | None = None) -> dict[str, Any]:
    """Phase 2 statistics over the scored rows (one row per case × trial).

    Pass and unsafe rates carry Wilson intervals; latency, tokens and cost carry
    median, p95 and a bootstrap CI. ``cost_per_success`` is total spend over the
    rows that reported a cost divided by the successes among them — the mean cost
    of a *successful* row would hide what the failures cost. The unsafe interval is
    over ``safety_rows`` (see :func:`_safety_rows`), defaulting to ``valid``.
    """
    n = len(valid)
    passes = sum(1 for s in valid if s.passed)
    safety_rows = valid if safety_rows is None else safety_rows
    unsafe = sum(1 for s in safety_rows if _is_unsafe(s))
    by_case: dict[str, list[float]] = {}
    for s in valid:
        by_case.setdefault(s.case_id, []).append(1.0 if s.passed else 0.0)
    priced = [s for s in valid if (s.usage or {}).get("cost_usd") is not None]
    priced_passes = sum(1 for s in priced if s.passed)
    return {
        "trials": max((s.trial_idx for s in valid), default=1),
        "cases_unique": len(by_case),
        "pass_rate_ci": stats.rate_summary(passes, n)["ci"],
        "pass_gate_status": stats.rate_summary(passes, n, gate=DOMAIN_PASS_RATE)["gate_status"],
        "unsafe_action_rate_ci": stats.rate_summary(unsafe, len(safety_rows))["ci"],
        # Mean per-case std dev of pass/fail across trials; None on a 1-trial run.
        "pass_consistency_std": stats.trial_consistency(by_case),
        # Only rows whose wall clock measures the model (see bench.nim).
        "latency_s": stats.distribution([s.latency_s for s in valid if s.latency_representative]),
        "latency_representative": all(s.latency_representative for s in valid),
        "total_tokens": stats.distribution(
            [s.usage["total_tokens"] for s in valid if (s.usage or {}).get("total_tokens") is not None],
            digits=1,
        ),
        "cost_usd": stats.distribution(
            [s.usage["cost_usd"] for s in priced], digits=6
        ),
        "cost_per_success": (
            round(sum(float(s.usage["cost_usd"]) for s in priced) / priced_passes, 6)
            if priced_passes
            else None
        ),
    }


def _compute_summary(model: str, scorecards: list[ScoreCard]) -> dict[str, Any]:
    if not scorecards:
        return {"model": model, "cases": 0}

    # A harness artifact is not a score. `_domain_breakdown` has always dropped
    # them; the headline did not, so six tick cases skipped by a market-warmup
    # timeout entered composite_avg and pass_rate as 0.0 and read as the model
    # failing them. One definition of "scored" for both.
    valid = [sc for sc in scorecards if sc.error is None and not sc.harness_artifact]
    n = len(valid) or 1
    with_tools = [s for s in valid if s.tool_accuracy is not None]
    with_params = [s for s in valid if s.tool_params is not None]
    with_validity = [s for s in valid if s.live_validity is not None]
    # answer_quality needs the same guard the three above have, and not having it
    # cost a whole 55-minute run. A judge that fails to answer yields None *without*
    # setting `error` — deliberately, since the tool evidence is still good and the
    # composite renormalises over what was measurable (bench.scorer) — so the row is
    # `valid`, and summing it raised TypeError one line after run_dir.mkdir(). The
    # run died with an empty directory: no summary, no cases, 80 cases of evidence
    # gone. `metrics.answer_quality.a_score` has two such paths, "No response
    # produced." and "Judge error (not scored): …", and one case hitting either was
    # enough to lose everything.
    with_quality = [s for s in valid if s.answer_quality is not None]
    unjudged = [s.case_id for s in valid if s.answer_quality is None]
    # Every row lands in exactly one bucket, so cases_total == cases_scored +
    # infra_excluded + harness_artifacts + errored. A harness_card is both
    # "infra: …" and a harness artifact and was counted twice; an error that was
    # neither (a crashed trial, a timeout without an artifact) was counted nowhere,
    # so the excluded rows did not add up to what was missing from the score.
    artifacts = [sc for sc in scorecards if sc.harness_artifact]
    infra_excluded = sum(
        1
        for sc in scorecards
        if not sc.harness_artifact and sc.error and str(sc.error).startswith("infra:")
    )
    errored = [
        sc
        for sc in scorecards
        if not sc.harness_artifact and sc.error and not str(sc.error).startswith("infra:")
    ]
    safety_rows = _safety_rows(scorecards, valid)
    unsafe_rows = [sc for sc in safety_rows if _is_unsafe(sc)]
    # Caveats are scored, so they are counted separately — a reader still needs to
    # know that every row in an ACP run was offered one extra tool.
    notes = [sc for sc in scorecards if getattr(sc, "harness_note", None)]
    unbuilt = [sc for sc in scorecards if getattr(sc, "post_condition_failed", None)]
    # Rows whose tool score reflects the agent reaching past an expected MCP tool
    # for one of its own built-ins. Scored as-is — see
    # `bench.scorer._detect_tool_substitutions` for why the number should not move —
    # but surfaced here, because six of these read as a capability gap in the
    # claude-code run and made directional_trader look like its weakest domain.
    substituted = [sc for sc in scorecards if getattr(sc, "tool_substitutions", None)]

    return {
        "model": model,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "cases_total": len(scorecards),
        "cases_scored": len(valid),
        # The four buckets partition cases_total: scored, infra-excluded, harness
        # artifact (whatever its error says), or errored for any other reason.
        "infra_excluded": infra_excluded,
        "harness_artifacts": len(artifacts),
        "cases_errored": len(errored),
        "errored_cases": [{"case_id": sc.case_id, "error": sc.error} for sc in errored],
        "harness_artifact_cases": [
            {"case_id": sc.case_id, "reason": sc.harness_artifact} for sc in artifacts
        ],
        # Scored, unlike artifacts — a comparability caveat, not an exclusion.
        "harness_notes": len(notes),
        "harness_note_reasons": sorted(
            {str(sc.harness_note) for sc in notes}
        ),
        # Cases that asserted an end state which was not there afterwards. Counted
        # separately from harness artifacts: those are excluded from routing, these
        # are genuine model failures whose composite was capped.
        "post_condition_failures": len(unbuilt),
        "post_condition_failure_cases": [
            {"case_id": sc.case_id, "reason": sc.post_condition_failed} for sc in unbuilt
        ],
        # Diagnostic, not an exclusion: the tool score stands, but a reader can see
        # that the miss was "used its own built-in" rather than "could not do it".
        "tool_substitutions": len(substituted),
        "tool_substitution_cases": [
            {"case_id": sc.case_id, "substituted": sc.tool_substitutions}
            for sc in substituted
        ],
        "answer_quality_avg": round(_mean([s.answer_quality for s in with_quality]), 4)
            if with_quality else None,
        # Scored on everything but the prose. Named, because a silent drop from the
        # quality average is exactly the kind of thin evidence this report exists to
        # make visible.
        "cases_unjudged": len(unjudged),
        "unjudged_cases": unjudged,
        "tool_accuracy_avg": round(_mean([s.tool_accuracy for s in with_tools]), 4)
            if with_tools else None,
        "tool_params_avg": round(_mean([s.tool_params for s in with_params]), 4)
            if with_params else None,
        "live_validity_avg": round(_mean([s.live_validity for s in with_validity]), 4)
            if with_validity else None,
        "latency_score_avg": (
            round(_mean([s.latency_score for s in valid if s.latency_score is not None]), 4)
            if any(s.latency_score is not None for s in valid)
            else None
        ),
        "composite_avg": round(_mean([s.composite for s in valid]), 4) if valid else 0.0,
        "latency_s_avg": round(sum(s.latency_s for s in valid) / n, 2) if valid else 0.0,
        "baseline_latency_s_avg": round(sum(s.baseline_latency_s for s in valid) / n, 2) if valid else 0.0,
        "pass_count": sum(1 for s in valid if s.passed),
        "pass_rate": round(sum(1 for s in valid if s.passed) / n, 4) if valid else 0.0,
        # §5 scorecard metrics
        "task_success_count": sum(1 for s in valid if s.task_success),
        "task_success_rate": round(sum(1 for s in valid if s.task_success) / n, 4) if valid else 0.0,
        # Over scored rows *plus* excluded rows that made a forbidden call: an
        # unsafe action counts whether or not the rest of the trial was scoreable.
        "unsafe_action_count": len(unsafe_rows),
        "unsafe_action_rate": round(len(unsafe_rows) / len(safety_rows), 4) if safety_rows else 0.0,
        "first_call_validity_avg": (
            round(_mean([s.first_call_validity for s in valid if s.first_call_validity is not None]), 4)
            if any(s.first_call_validity is not None for s in valid)
            else None
        ),
        "repair_rate_avg": (
            round(_mean([s.repair_rate for s in valid if s.repair_rate is not None]), 4)
            if any(s.repair_rate is not None for s in valid)
            else None
        ),
        "tool_calls_per_success_avg": (
            round(_mean([s.tool_calls_per_success for s in valid if s.tool_calls_per_success is not None]), 2)
            if any(s.tool_calls_per_success is not None for s in valid)
            else None
        ),
        "cost_per_success_avg": (
            round(_mean([s.cost_per_success for s in valid if s.cost_per_success is not None]), 6)
            if any(s.cost_per_success is not None for s in valid)
            else None
        ),
        **_trial_stats(valid, safety_rows),
        "task_categories": _group_rates(valid, "task_category"),
        "splits": _group_rates(valid, "split"),
        # Regression tasks that failed in this run. Each one is a drop to look at:
        # the split exists so a release cannot quietly lose ground it held.
        "regression_failures": sorted(
            {s.task_id or s.case_id for s in valid if s.split == "regression" and not s.passed}
        ),
        "usage": _usage_totals(scorecards),
        "judge_usage": _judge_totals(scorecards),
        "domains": _domain_breakdown(scorecards),
    }


def load_all_runs() -> list[dict[str, Any]]:
    """Load all run summaries from results/ for the dashboard."""
    summaries = []
    for summary_file in sorted(RESULTS_DIR.glob("*/summary.json")):
        try:
            data = json.loads(summary_file.read_text())
            data["run_dir"] = str(summary_file.parent.name)
            summaries.append(data)
        except Exception:
            continue
    return summaries


def load_run_cases(run_dir_name: str) -> list[dict[str, Any]]:
    """Load all case results from a specific run."""
    cases_dir = RESULTS_DIR / run_dir_name / "cases"
    if not cases_dir.exists():
        return []
    records = []
    for f in sorted(cases_dir.glob("*.json")):
        try:
            records.append(json.loads(f.read_text()))
        except Exception:
            continue
    return records
