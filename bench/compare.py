"""N-way Compare for suite run-group members — sound by default.

One variable under test: members may differ in exactly one *variable* —
the model, the Condor checkout, or a single manifest key — and Compare refuses
when more than one differs, listing every one. The model and its inference
config count as one variable (Experiment B lets each model use its own params);
the Condor commit and the contract version it ships count as another
(Experiment A). Runs without a ``manifest`` block (pre-Phase 0) are compared on
model and checkout alone.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bench import stats
from bench.matrix import summary_lane
from config import PASS_THRESHOLD, RESULTS_DIR


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


# ── Manifest diff ──────────────────────────────────────────────────────────────
# Keys within the manifest block that are compared.  ``runtime`` is included
# because different timeouts or concurrency can change outcomes.
_MANIFEST_COMPARE_KEYS = (
    "contract_version",
    "task_set_version",
    "grader_version",
    "inference_config",
    "env_mode",
    "experiment_type",
    "uv_lock_hash",
    "trials",
    "runtime",
)


def diff_manifests(members: list[dict[str, Any]]) -> list[str]:
    """Return the manifest keys that differ across *members*.

    Returns an empty list when all manifests agree (or when none of the
    members have a manifest block — backwards compatibility).
    """
    manifests = [m.get("manifest") or {} for m in members]
    if all(not m for m in manifests):
        return []

    diffs: list[str] = []
    for key in _MANIFEST_COMPARE_KEYS:
        values = {json.dumps(m.get(key), sort_keys=True, default=str) for m in manifests}
        if len(values) > 1:
            diffs.append(key)
    return diffs


# Manifest keys that travel with another variable rather than counting alone.
_VARIABLE_OF_KEY = {
    "inference_config": "model",
    "contract_version": "condor_checkout",
    "task_set_version": "case_set",
}


def _condor_identity(summary: dict) -> str | None:
    """Commit + dirty state of the checkout a run measured, or None if unrecorded."""
    condor = summary.get("condor") or {}
    commit = condor.get("commit")
    if not commit:
        return None
    return f"{commit}{'+dirty' if condor.get('dirty_files') else ''}"


def varied_variables(members: list[dict[str, Any]]) -> list[str]:
    """The distinct variables that differ across *members*, in a stable order."""
    varied: list[str] = []

    def _add(name: str) -> None:
        if name not in varied:
            varied.append(name)

    if len({tuple(m.get("models") or [m.get("model")]) for m in members}) > 1:
        _add("model")
    condors = {_condor_identity(m) for m in members}
    if len(condors) > 1 and None not in condors:
        _add("condor_checkout")
    if len({_case_set(m) for m in members}) > 1:
        _add("case_set")
    for key in diff_manifests(members):
        _add(_VARIABLE_OF_KEY.get(key, f"manifest:{key}"))
    return varied


def compare_summaries(
    members: list[dict[str, Any]],
    cases: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Compare 2+ member summaries. Returns comparable + differences + deltas.

    ``differences`` holds the reasons a comparison was refused and is empty when
    it is comparable; ``variable_under_test`` names the one variable that
    differs, or None when the members are identical.
    """
    if len(members) < 2:
        return {
            "comparable": False,
            "differences": ["need_at_least_two_members"],
            "members": members,
            "deltas": None,
        }

    differences: list[str] = []

    if any(int(m.get("harness_artifacts") or 0) > 0 for m in members):
        differences.append("harness_artifacts")

    statuses = {m.get("member_status") for m in members if m.get("member_status")}
    if "skipped" in statuses or "failed" in statuses:
        differences.append("partial_group")

    varied = varied_variables(members)
    # Not variables under test. A 1-trial and a 3-trial run measure the same thing
    # with different precision, so their rates cannot be differenced; a mock and a
    # live run measured different worlds.
    for hard in ("manifest:trials", "manifest:env_mode"):
        if hard in varied:
            differences.append(hard)
            varied = [v for v in varied if v != hard]
    lanes = {summary_lane(m) for m in members}
    if None in lanes and "manifest:env_mode" not in differences:
        differences.append("unknown_lane")
    if len(varied) > 1:
        differences.extend(varied)
    manifest_diffs = diff_manifests(members)

    comparable = not differences
    deltas = _build_deltas(members, cases) if comparable else None

    return {
        "comparable": comparable,
        "differences": differences,
        "variable_under_test": varied[0] if len(varied) == 1 else None,
        "varied": varied,
        "manifest_diffs": manifest_diffs,
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
                "manifest": m.get("manifest"),
            }
            for m in members
        ],
        "deltas": deltas,
    }


def _scored_by_trial(records: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Scored rows keyed ``case_id#trial`` — the pairing unit for A/B deltas."""
    composite: dict[str, float] = {}
    passed: dict[str, float] = {}
    for r in records:
        if r.get("error") or r.get("harness_artifact"):
            continue
        key = f"{r.get('case_id')}#{int(r.get('trial_idx') or 1)}"
        composite[key] = float(r.get("composite") or 0.0)
        verdict = r.get("passed")
        if verdict is None:  # saved before `passed` was persisted
            verdict = composite[key] >= PASS_THRESHOLD
        passed[key] = 1.0 if verdict else 0.0
    return {"composite": composite, "passed": passed}


def paired_deltas(
    base_records: list[dict[str, Any]], other_records: list[dict[str, Any]]
) -> dict[str, Any]:
    """Per-(case, trial) paired differences, other minus base, with bootstrap CIs.

    Pairing on the same task and trial index removes between-task variance, which
    is what lets a 3-trial A/B on a few dozen tasks resolve a real difference.
    Rows scored on only one side are left out and counted.
    """
    base = _scored_by_trial(base_records)
    other = _scored_by_trial(other_records)
    out: dict[str, Any] = {}
    for metric in ("composite", "passed"):
        out[metric] = stats.paired_deltas(base[metric], other[metric])
    out["unpaired"] = len(set(base["composite"]) ^ set(other["composite"]))
    return out


def _build_deltas(
    members: list[dict[str, Any]],
    cases: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
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
            "paired": (
                paired_deltas(
                    cases.get(str(base.get("run_dir")), []),
                    cases.get(str(other.get("run_dir")), []),
                )
                if cases
                else None
            ),
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
    from bench.reporter import load_run_cases

    cases = {str(m.get("run_dir")): load_run_cases(str(m.get("run_dir"))) for m in members}
    return compare_summaries(members, cases)
