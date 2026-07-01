"""Generate and persist benchmark run reports."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from config import RESULTS_DIR
from bench.scorer import ScoreCard


def save_run(
    model: str,
    scorecards: list[ScoreCard],
    responses: dict[str, str],
    run_id: str | None = None,
) -> Path:
    """Persist a full benchmark run to results/{run_id}/."""
    run_id = run_id or datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    safe_model = model.replace(":", "_").replace("/", "_")
    run_dir = RESULTS_DIR / f"{run_id}_{safe_model}"
    run_dir.mkdir(parents=True, exist_ok=True)

    summary = _compute_summary(model, scorecards)
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))

    cases_dir = run_dir / "cases"
    cases_dir.mkdir(exist_ok=True)
    for sc in scorecards:
        record = sc.as_dict()
        record["response"] = responses.get(sc.case_id, "")
        (cases_dir / f"{sc.case_id}.json").write_text(json.dumps(record, indent=2))

    return run_dir


def _compute_summary(model: str, scorecards: list[ScoreCard]) -> dict[str, Any]:
    if not scorecards:
        return {"model": model, "cases": 0}

    valid = [sc for sc in scorecards if sc.error is None]
    n = len(valid) or 1

    return {
        "model": model,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "cases_total": len(scorecards),
        "cases_scored": len(valid),
        "answer_quality_avg": round(sum(s.answer_quality for s in valid) / n, 4),
        "tool_accuracy_avg": round(sum(s.tool_accuracy for s in valid if s.tool_accuracy is not None) / n, 4)
            if any(s.tool_accuracy is not None for s in valid) else None,
        "latency_score_avg": round(sum(s.latency_score for s in valid) / n, 4),
        "composite_avg": round(sum(s.composite for s in valid) / n, 4),
        "latency_s_avg": round(sum(s.latency_s for s in valid) / n, 2),
        "baseline_latency_s_avg": round(sum(s.baseline_latency_s for s in valid) / n, 2),
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
