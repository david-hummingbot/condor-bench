"""Generate and persist benchmark run reports."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import PASS_THRESHOLD, RESULTS_DIR
from bench.scorer import ScoreCard


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

    summary = _compute_summary(model, scorecards)
    if extra_summary:
        summary.update(extra_summary)
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))

    cases_dir = run_dir / "cases"
    cases_dir.mkdir(exist_ok=True)
    for sc in scorecards:
        record = sc.as_dict()
        record["response"] = responses.get(sc.case_id, "")
        if prompts and sc.case_id in prompts:
            record["question"] = prompts[sc.case_id]
        (cases_dir / f"{sc.case_id}.json").write_text(json.dumps(record, indent=2))

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
        passed = [c for c in scorable if c.composite >= PASS_THRESHOLD]
        out[domain] = {
            "cases": len(cards),
            "scored": len(scorable),
            "excluded": len(cards) - len(scorable),
            "pass_rate": round(len(passed) / len(scorable), 4),
            "avg_composite": round(_mean([c.composite for c in scorable]), 4),
        }
    return out


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
    infra_excluded = sum(1 for sc in scorecards if sc.error and str(sc.error).startswith("infra:"))
    artifacts = [sc for sc in scorecards if sc.harness_artifact]
    # Caveats are scored, so they are counted separately — a reader still needs to
    # know that every row in an ACP run was offered one extra tool.
    notes = [sc for sc in scorecards if getattr(sc, "harness_note", None)]
    unbuilt = [sc for sc in scorecards if getattr(sc, "post_condition_failed", None)]

    return {
        "model": model,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "cases_total": len(scorecards),
        "cases_scored": len(valid),
        "infra_excluded": infra_excluded,
        "harness_artifacts": len(artifacts),
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
        "answer_quality_avg": round(_mean([s.answer_quality for s in valid]), 4) if valid else 0.0,
        "tool_accuracy_avg": round(_mean([s.tool_accuracy for s in with_tools]), 4)
            if with_tools else None,
        "tool_params_avg": round(_mean([s.tool_params for s in with_params]), 4)
            if with_params else None,
        "live_validity_avg": round(_mean([s.live_validity for s in with_validity]), 4)
            if with_validity else None,
        "latency_score_avg": round(_mean([s.latency_score for s in valid]), 4) if valid else 0.0,
        "composite_avg": round(_mean([s.composite for s in valid]), 4) if valid else 0.0,
        "latency_s_avg": round(sum(s.latency_s for s in valid) / n, 2) if valid else 0.0,
        "baseline_latency_s_avg": round(sum(s.baseline_latency_s for s in valid) / n, 2) if valid else 0.0,
        "pass_count": sum(1 for s in valid if s.composite >= PASS_THRESHOLD),
        "pass_rate": round(sum(1 for s in valid if s.composite >= PASS_THRESHOLD) / n, 4)
            if valid else 0.0,
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
