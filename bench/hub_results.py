"""Hub-agent results grouped by agent, then by model.

`runner.py hub` writes one run directory per (agent, model) like any other run,
so the Runs list shows them one at a time and the Leaderboard ranks models
across everything. Neither answers "which models have been run against this
agent, and how did each do?" — this module does.

Scores are pooled over case records, not averaged over run summaries: two runs
of 4 cases and one of 8 trials should weigh by cases, and a case's per-model
score needs the records anyway. A record counts as scored by the same rule the
reporter uses for `cases_scored` — no error and no harness artifact — so a run
whose every case hit a harness fault (a hung endpoint, a self-grading refusal)
shows up as "not scored" instead of averaging in as 0.0.

Only runs of the agent's current version count. An agent republished on the
hub is a different agent; its older runs stay listed, marked, but out of the
scores.
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean
from typing import Any


def _scored(record: dict[str, Any]) -> bool:
    return record.get("error") is None and not record.get("harness_artifact")


def _avg(values: list[float]) -> float | None:
    vals = [float(v) for v in values if v is not None]
    return round(mean(vals), 4) if vals else None


def _load_runs(results_dir: Path) -> list[dict[str, Any]]:
    runs = []
    for summary_file in results_dir.glob("*/summary.json"):
        try:
            summary = json.loads(summary_file.read_text())
        except (OSError, ValueError):
            continue
        hub = summary.get("hub_agent")
        if summary.get("run_type") != "hub_agent" or not isinstance(hub, dict) or not hub.get("slug"):
            continue
        records = []
        for f in sorted((summary_file.parent / "cases").glob("*.json")):
            try:
                records.append(json.loads(f.read_text()))
            except (OSError, ValueError):
                continue
        runs.append({"dir": summary_file.parent.name, "summary": summary, "records": records})
    runs.sort(key=lambda r: r["summary"].get("timestamp") or "")
    return runs


def _case_label(case_id: str) -> str:
    """`hub_churn__loop_stable_churn_supervisor` -> `loop stable_churn_supervisor`."""
    rest = case_id.split("__", 1)[1] if "__" in case_id else case_id
    if rest == "state_check":
        return "state check"
    kind, _, name = rest.partition("_")
    return f"{kind} {name}" if name else rest


def _model_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [r for r in records if _scored(r)]
    passed = [r for r in scored if r.get("passed")]
    priced = [r for r in scored if (r.get("usage") or {}).get("cost_usd") is not None]
    return {
        "cases_total": len(records),
        "cases_scored": len(scored),
        "composite": _avg([r.get("composite") for r in scored]),
        "answer_quality": _avg([r.get("answer_quality") for r in scored]),
        "tool_accuracy": _avg([r.get("tool_accuracy") for r in scored]),
        "pass_rate": round(len(passed) / len(scored), 4) if scored else None,
        "passed": len(passed),
        "model_time_per_success": _avg([r.get("model_time_s") for r in passed]),
        "tool_calls_per_success": _avg([len(r.get("tool_calls") or []) for r in passed]),
        # Over priced rows only, like the reporter: a model with no price data
        # reads "—", never $0.
        "cost_per_success": (
            round(sum(float(r["usage"]["cost_usd"]) for r in priced) / len(passed), 6)
            if priced and passed
            else None
        ),
        "unsafe": sum(1 for r in scored if r.get("forbidden_violations")),
    }


def _run_row(run: dict[str, Any], current_digest: str | None) -> dict[str, Any]:
    s = run["summary"]
    hub = s["hub_agent"]
    scored = [r for r in run["records"] if _scored(r)]
    reasons = sorted({
        str(r.get("harness_artifact") or r.get("error"))[:200]
        for r in run["records"]
        if not _scored(r)
    })
    return {
        "run_dir": run["dir"],
        "model": s.get("model"),
        "timestamp": s.get("timestamp"),
        "trials": s.get("trials"),
        "cases_total": len(run["records"]),
        "cases_scored": len(scored),
        "composite": _avg([r.get("composite") for r in scored]),
        "pass_rate": (
            round(sum(1 for r in scored if r.get("passed")) / len(scored), 4) if scored else None
        ),
        "current_version": hub.get("digest") == current_digest,
        "execute": bool(hub.get("execute")),
        "not_scored_reasons": reasons,
    }


def hub_agent_board(results_dir: Path) -> dict[str, Any]:
    by_slug: dict[str, list[dict[str, Any]]] = {}
    for run in _load_runs(results_dir):
        by_slug.setdefault(run["summary"]["hub_agent"]["slug"], []).append(run)

    agents = []
    for slug, runs in by_slug.items():
        latest = runs[-1]["summary"]
        hub = latest["hub_agent"]
        digest = hub.get("digest")
        current = [r for r in runs if r["summary"]["hub_agent"].get("digest") == digest]

        by_model: dict[str, list[dict[str, Any]]] = {}
        for run in current:
            by_model.setdefault(run["summary"].get("model") or "?", []).append(run)

        models = []
        cases: dict[str, dict[str, Any]] = {}
        for model, model_runs in by_model.items():
            records = [rec for run in model_runs for rec in run["records"]]
            stats = _model_stats(records)
            stats.update({
                "model": model,
                "runs": len(model_runs),
                "runs_not_scored": sum(
                    1 for run in model_runs if not any(_scored(r) for r in run["records"])
                ),
                "last_run": model_runs[-1]["summary"].get("timestamp"),
            })
            models.append(stats)
            for rec in records:
                cid = rec.get("case_id") or "?"
                case = cases.setdefault(cid, {
                    "case_id": cid,
                    "label": _case_label(cid),
                    "case_type": rec.get("case_type"),
                    "by_model": {},
                })
                case["by_model"].setdefault(model, []).append(rec)

        for case in cases.values():
            case["by_model"] = {
                model: {
                    "composite": _avg([r.get("composite") for r in recs if _scored(r)]),
                    "answer_quality": _avg([r.get("answer_quality") for r in recs if _scored(r)]),
                    "passed": sum(1 for r in recs if _scored(r) and r.get("passed")),
                    "scored": sum(1 for r in recs if _scored(r)),
                    "trials": len(recs),
                }
                for model, recs in case["by_model"].items()
            }

        # Unscored models sink to the bottom rather than ranking as 0.
        models.sort(key=lambda m: (m["composite"] is None, -(m["composite"] or 0), m["model"]))
        agents.append({
            "slug": slug,
            "name": hub.get("name") or slug,
            "version": hub.get("version"),
            "digest": digest,
            "source": hub.get("source"),
            "skills": hub.get("skills") or [],
            "loops": hub.get("loops") or [],
            "code_files_skipped": hub.get("code_files_skipped") or [],
            "findings": hub.get("findings") or [],
            "last_run": latest.get("timestamp"),
            "models": models,
            "cases": sorted(cases.values(), key=lambda c: c["case_id"]),
            "runs": [_run_row(r, digest) for r in reversed(runs)],
        })

    agents.sort(key=lambda a: a["last_run"] or "", reverse=True)
    return {"agents": agents}
