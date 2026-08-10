"""N-way Compare for suite run-group members — sound by default."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from config import RESULTS_DIR


def load_summary(run_dir: str | Path) -> dict[str, Any]:
    path = RESULTS_DIR / str(run_dir) / "summary.json"
    if not path.is_file():
        raise FileNotFoundError(f"No summary for run_dir={run_dir}")
    data = json.loads(path.read_text())
    data["run_dir"] = path.parent.name
    return data


def load_run_group(run_group_id: str) -> list[dict[str, Any]]:
    members = []
    for summary_path in RESULTS_DIR.glob("*/summary.json"):
        try:
            data = json.loads(summary_path.read_text())
        except Exception:
            continue
        if data.get("run_group_id") == run_group_id:
            data["run_dir"] = summary_path.parent.name
            members.append(data)
    return sorted(members, key=lambda m: (m.get("environment_id") or "", m.get("model") or ""))


def _case_set(summary: dict) -> tuple[str, ...]:
    ids = summary.get("case_ids") or []
    return tuple(sorted(str(i) for i in ids))


def compare_summaries(members: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare 2+ member summaries. Returns comparable + differences + deltas."""
    if len(members) < 2:
        return {
            "comparable": False,
            "differences": ["need_at_least_two_members"],
            "members": members,
            "deltas": None,
        }

    differences: list[str] = []

    models = {tuple(m.get("models") or [m.get("model")]) for m in members}
    if len(models) > 1:
        differences.append("model")

    case_sets = {_case_set(m) for m in members}
    if len(case_sets) > 1:
        differences.append("case_set")

    ceilings = {m.get("risk_ceiling") for m in members}
    if len(ceilings) > 1:
        differences.append("risk_ceiling")

    mutating = {bool(m.get("allow_mutating")) for m in members}
    if len(mutating) > 1:
        differences.append("allow_mutating")

    if any(int(m.get("harness_artifacts") or 0) > 0 for m in members):
        differences.append("harness_artifacts")

    statuses = {m.get("member_status") for m in members if m.get("member_status")}
    if "skipped" in statuses or "failed" in statuses:
        differences.append("partial_group")

    comparable = not differences
    deltas = _build_deltas(members) if comparable else None

    return {
        "comparable": comparable,
        "differences": differences,
        "members": [
            {
                "run_dir": m.get("run_dir"),
                "environment_id": m.get("environment_id"),
                "suite_id": m.get("suite_id"),
                "model": m.get("model"),
                "pass_rate": m.get("pass_rate"),
                "composite_avg": m.get("composite_avg"),
                "latency_s_avg": m.get("latency_s_avg"),
                "cases_scored": m.get("cases_scored"),
                "n": m.get("cases_scored") or m.get("cases_total"),
                "condor": m.get("condor"),
            }
            for m in members
        ],
        "deltas": deltas,
    }


def _build_deltas(members: list[dict[str, Any]]) -> dict[str, Any]:
    """Pairwise deltas vs the first member (baseline)."""
    base = members[0]
    base_n = int(base.get("cases_scored") or base.get("cases_total") or 0)
    out: dict[str, Any] = {"baseline_run_dir": base.get("run_dir"), "pairs": []}
    for other in members[1:]:
        other_n = int(other.get("cases_scored") or other.get("cases_total") or 0)
        pair = {
            "run_dir": other.get("run_dir"),
            "environment_id": other.get("environment_id"),
            "pass_rate_delta": _sub(other.get("pass_rate"), base.get("pass_rate")),
            "composite_avg_delta": _sub(
                other.get("composite_avg"), base.get("composite_avg")
            ),
            "latency_s_avg_delta": _sub(
                other.get("latency_s_avg"), base.get("latency_s_avg")
            ),
            "latency_n": {"baseline": base_n, "compare": other_n},
            "avg_total_tokens_delta": _usage_delta(base, other),
            "domains": _domain_deltas(base, other),
        }
        out["pairs"].append(pair)
    return out


def _sub(a: Any, b: Any) -> float | None:
    if a is None or b is None:
        return None
    try:
        return round(float(a) - float(b), 4)
    except (TypeError, ValueError):
        return None


def _usage_delta(base: dict, other: dict) -> float | None:
    bu = (base.get("usage") or {}).get("avg_total_tokens")
    ou = (other.get("usage") or {}).get("avg_total_tokens")
    return _sub(ou, bu)


def _domain_deltas(base: dict, other: dict) -> dict[str, Any]:
    bd = base.get("domains") or {}
    od = other.get("domains") or {}
    keys = sorted(set(bd) | set(od))
    out: dict[str, Any] = {}
    for key in keys:
        b = bd.get(key) or {}
        o = od.get(key) or {}
        out[key] = {
            "pass_rate_delta": _sub(o.get("pass_rate"), b.get("pass_rate")),
            "avg_composite_delta": _sub(o.get("avg_composite"), b.get("avg_composite")),
            "n_baseline": b.get("scored"),
            "n_compare": o.get("scored"),
        }
    return out


def compare_runs(
    *,
    run_group_id: str | None = None,
    run_dirs: list[str] | None = None,
) -> dict[str, Any]:
    if run_group_id:
        members = load_run_group(run_group_id)
    elif run_dirs:
        members = [load_summary(d) for d in run_dirs]
    else:
        return {
            "comparable": False,
            "differences": ["no_inputs"],
            "members": [],
            "deltas": None,
        }
    return compare_summaries(members)
