"""Aggregate benchmark runs into a model × task matrix.

Reads ``results/*/summary.json`` + ``results/*/cases/*.json`` and produces the
structure the dashboard heatmap and ``bench/routing.py`` both consume: per model,
a pass rate and mean composite for every routing domain and every individual MCP
tool, plus token and cost columns.

Three rules that decide what a cell actually means:

**One run per cell — the newest that covered it.** Averaging a model's runs
together would blend a run made before a prompt fix with one made after, and the
number would move for reasons unrelated to the model. But "newest run per model"
would be wrong too: the documented workflow sweeps domain by domain
(``sweep -d market_making_expert``), and that would erase every other domain for that
model. So resolution is per *(model, cell)*: each cell takes the newest run that
actually has cases for it, and cells carry ``run_dir`` so a surprising value can
be traced back to the run that produced it. "Has cases" means scored (or unsafe)
cases where any run has them: a newer run whose every row was excluded — a staging
outage — only owns a cell no run ever scored.

**Infra failures and harness artifacts are excluded, not scored 0.** A case that
failed because staging was unreachable, or because it ran chat-scoped when it
needed ``--agent-slug``, says nothing about the model. Excluded counts are
reported per cell — a cell resting on two of eight cases is not the same evidence
as one resting on all eight, and the heatmap needs to be able to say so.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from bench import stats
from bench.atomic_io import atomic_write_text
from config import (
    DATASETS_DIR,
    DESTRUCTIVE_FLOOR,
    DOMAIN_PASS_RATE,
    PASS_THRESHOLD,
    RESULTS_DIR,
    TOOL_PASS_RATE,
)
from metrics.tool_accuracy import normalize_tool_name


# ── Model registry ─────────────────────────────────────────────────────────────
@dataclass
class ModelEntry:
    key: str
    params_b: float | None = None
    provider: str = "local"
    tool_filter_mode: str | None = None
    notes: str = ""

    @property
    def sort_key(self) -> tuple[int, float, str]:
        """Ascending by size; cloud models (params_b None) always last.

        Routing wants the smallest model that works, and "unknown size" is not a
        licence to recommend a cloud model over a local one that also passes.
        """
        if self.params_b is None:
            return (1, float("inf"), self.key)
        return (0, float(self.params_b), self.key)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "params_b": self.params_b,
            "provider": self.provider,
            "tool_filter_mode": self.tool_filter_mode,
            "notes": self.notes,
        }


def load_models(path: Path | None = None) -> list[ModelEntry]:
    """Load the model registry. Accepts the documented object form or a bare list."""
    path = path or DATASETS_DIR / "models.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    rows = data.get("models", []) if isinstance(data, dict) else data
    entries = [
        ModelEntry(
            key=row["key"],
            params_b=row.get("params_b"),
            provider=row.get("provider", "local"),
            tool_filter_mode=row.get("tool_filter_mode"),
            notes=row.get("notes", ""),
        )
        for row in rows
        if isinstance(row, dict) and row.get("key")
    ]
    return sorted(entries, key=lambda e: e.sort_key)


def model_index(models: Iterable[ModelEntry] | None = None) -> dict[str, ModelEntry]:
    return {m.key: m for m in (models if models is not None else load_models())}


# ── Cell aggregation ───────────────────────────────────────────────────────────
@dataclass
class Cell:
    cases: int = 0
    scored: int = 0
    excluded: int = 0
    passed: int = 0
    composites: list[float] = field(default_factory=list)
    total_tokens: list[float] = field(default_factory=list)
    costs: list[float] = field(default_factory=list)
    latencies: list[float] = field(default_factory=list)
    # Destructive cases below the floor block a routing recommendation outright.
    destructive_failures: list[str] = field(default_factory=list)
    excluded_reasons: list[str] = field(default_factory=list)
    run_dir: str = ""
    # Cases whose tool surface was cut by the per-mode cap. A tool the model was
    # never shown cannot be evidence about the model, and "we withheld it" has to
    # be distinguishable from "it failed" — under `moderate` (12 of 25 tools) that
    # is 42 of the 80 cases, and the positional cut means the same half of the
    # surface is invisible to every capped model.
    withheld: int = 0
    # Rows with a forbidden or unconfirmed write — scored *or excluded*. The router
    # requires zero. An excluded row is still counted: exclusion says the harness
    # spoiled the score, not that the order was never placed (see
    # `bench.trials.flag_mock_gaps`, which refuses the same laundering).
    unsafe: int = 0
    # How many of `unsafe` are excluded rows, so the rate's denominator can include
    # them without counting every excluded row as a safe trial.
    unsafe_excluded: int = 0
    # Per-case pass/fail across trials, for the consistency figure.
    outcomes: dict[str, list[float]] = field(default_factory=dict)
    # Successes among the rows that reported a cost, so cost per success divides
    # spend by the successes that spend actually bought.
    priced_passes: int = 0

    def add(self, case: dict) -> None:
        self.cases += 1
        if (case.get("wiring") or {}).get("tools_truncated"):
            self.withheld += 1
        reason = _exclusion_reason(case)
        if _is_unsafe(case):
            # Before the exclusion return: an infra failure after a banned call used
            # to drop the row here, and the cell (and so the router) read it as safe.
            self.unsafe += 1
            if reason:
                self.unsafe_excluded += 1
        if reason:
            self.excluded += 1
            self.excluded_reasons.append(f"{case.get('case_id')}: {reason}")
            return

        self.scored += 1
        composite = float(case.get("composite") or 0.0)
        self.composites.append(composite)
        passed = _case_passed(case, composite)
        if passed:
            self.passed += 1
        self.outcomes.setdefault(str(case.get("case_id")), []).append(1.0 if passed else 0.0)
        if case.get("risk_level") == "destructive" and composite < DESTRUCTIVE_FLOOR:
            self.destructive_failures.append(str(case.get("case_id")))

        usage = case.get("usage") or {}
        if usage.get("total_tokens") is not None:
            self.total_tokens.append(float(usage["total_tokens"]))
        if usage.get("cost_usd") is not None:
            self.costs.append(float(usage["cost_usd"]))
            if passed:
                self.priced_passes += 1
        # A NIM row's wall clock measures the hosted endpoint, not the model.
        if case.get("latency_s") is not None and case.get("latency_representative", True):
            self.latencies.append(float(case["latency_s"]))

    @property
    def _safety_rows(self) -> int:
        """Scored rows plus the excluded rows that were unsafe anyway."""
        return self.scored + self.unsafe_excluded

    def as_dict(self, gate: float = DOMAIN_PASS_RATE) -> dict[str, Any]:
        rate = stats.rate_summary(self.passed, self.scored, gate=gate)
        latency = stats.distribution(self.latencies)
        return {
            "cases": self.cases,
            "scored": self.scored,
            "excluded": self.excluded,
            "excluded_reasons": self.excluded_reasons[:8],
            "withheld": self.withheld,
            "passed": self.passed,
            "pass_rate": round(self.passed / self.scored, 4) if self.scored else None,
            # Wilson 95% interval over every scored trial, and where it sits against
            # this axis's gate: "not_yet_passing" means the interval straddles it —
            # more trials could settle it either way, which is not the same as failing.
            "pass_rate_ci": rate["ci"],
            "gate": gate,
            "gate_status": rate["gate_status"],
            "unsafe": self.unsafe,
            "unsafe_excluded": self.unsafe_excluded,
            "unsafe_rate": (
                round(self.unsafe / self._safety_rows, 4) if self._safety_rows else None
            ),
            "unsafe_rate_ci": stats.rate_summary(self.unsafe, self._safety_rows)["ci"],
            "trials": max((len(v) for v in self.outcomes.values()), default=0),
            "consistency_std": stats.trial_consistency(self.outcomes),
            "cost_per_success": (
                round(sum(self.costs) / self.priced_passes, 6) if self.priced_passes else None
            ),
            "median_latency_s": latency["median"] if latency else None,
            "p95_latency_s": latency["p95"] if latency else None,
            "latency_s_ci": latency["ci"] if latency else None,
            "total_tokens_ci": (
                None if (d := stats.distribution(self.total_tokens, digits=1)) is None else d["ci"]
            ),
            "avg_composite": _round_mean(self.composites, 4),
            # None, not 0: an unmeasured token count is not a free one, and the
            # routing tie-breaker must be able to tell the difference.
            "avg_total_tokens": _round_mean(self.total_tokens, 1),
            "p95_total_tokens": _p95(self.total_tokens),
            "avg_cost_usd": _round_mean(self.costs, 6),
            "avg_latency_s": _round_mean(self.latencies, 2),
            "destructive_failures": self.destructive_failures,
            "run_dir": self.run_dir,
        }


def _case_passed(case: dict, composite: float) -> bool:
    """The case record's own verdict, which carries the hard gates.

    Records saved before ``passed`` was persisted fall back to the threshold.
    """
    if "passed" in case and case["passed"] is not None:
        return bool(case["passed"])
    return composite >= PASS_THRESHOLD


def _exclusion_reason(case: dict) -> str | None:
    """Why this case can't be evidence about the model, or None if it can."""
    if case.get("harness_artifact"):
        return str(case["harness_artifact"])
    error = case.get("error")
    if error:
        return str(error)
    return None


def _is_unsafe(case: dict) -> bool:
    return bool(case.get("forbidden_violations")) or float(
        case.get("unsafe_action_rate") or 0.0
    ) > 0.0


def _is_evidence(case: dict) -> bool:
    """True when the row says something about the model: it was scored, or it made
    an unsafe call (which is evidence however the rest of the trial went)."""
    return _exclusion_reason(case) is None or _is_unsafe(case)


def _round_mean(values: list[float], digits: int) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), digits)


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(0.95 * (len(ordered) - 1))))
    return round(ordered[idx], 1)


# ── Run loading ────────────────────────────────────────────────────────────────
@dataclass
class Run:
    run_dir: str
    model: str
    timestamp: str
    cases: list[dict]


def summary_lane(summary: dict) -> str | None:
    """The lane a persisted run was measured on, or None when it cannot be told.

    A summary with no manifest predates Phase 0 — and the mock lane — so it can
    only have been live. A manifest without ``env_mode`` matches neither lane:
    every manifest since the lane existed records it, so its absence means the
    run's provenance is unknown, and mock and live rows must never share a frontier.
    """
    manifest = summary.get("manifest")
    if not manifest:
        return "live"
    mode = manifest.get("env_mode")
    return mode if mode in ("live", "mock") else None


def load_runs(results_dir: Path | None = None, *, lane: str = "live") -> list[Run]:
    """Every persisted run eligible for *lane*'s matrix, newest first.

    Suite and custom-prompt runs are excluded unless ``include_in_matrix`` is set
    — see :func:`config.summary_counts_for_matrix` — and a run from the other lane
    is never eligible (see :func:`summary_lane`).
    """
    from config import summary_counts_for_matrix

    results_dir = results_dir or RESULTS_DIR
    runs: list[Run] = []
    for summary_path in results_dir.glob("*/summary.json"):
        try:
            summary = json.loads(summary_path.read_text())
        except Exception:
            continue
        if not summary_counts_for_matrix(summary):
            continue
        if summary_lane(summary) != lane:
            continue
        run_dir = summary_path.parent
        cases = []
        for case_file in sorted((run_dir / "cases").glob("*.json")):
            try:
                cases.append(json.loads(case_file.read_text()))
            except Exception:
                continue
        runs.append(
            Run(
                run_dir=run_dir.name,
                model=str(summary.get("model", "")),
                timestamp=str(summary.get("timestamp", "")),
                cases=cases,
            )
        )
    # Fall back to directory name when a summary predates the timestamp field, so
    # "latest" stays well-defined for old runs rather than picking arbitrarily.
    return sorted(runs, key=lambda r: (r.timestamp, r.run_dir), reverse=True)


def latest_run_per_model(runs: list[Run]) -> dict[str, Run]:
    """Newest run per model.

    Used for the per-model metadata row (which run/timestamp to show). Cell values
    resolve per (model, cell) instead — see :func:`build_matrix`.
    """
    latest: dict[str, Run] = {}
    for run in runs:  # already newest-first
        if run.model and run.model not in latest:
            latest[run.model] = run
    return latest


# ── Matrix ─────────────────────────────────────────────────────────────────────
def build_matrix(
    *,
    results_dir: Path | None = None,
    models_path: Path | None = None,
    lane: str = "live",
) -> dict[str, Any]:
    """Build *lane*'s model × (domain, tool) matrix from persisted runs."""
    registry = model_index(load_models(models_path))
    all_runs = [r for r in load_runs(results_dir, lane=lane) if r.model]
    latest = latest_run_per_model(all_runs)

    domains: dict[str, dict[str, Cell]] = {}
    tools: dict[str, dict[str, Cell]] = {}
    # Keyed like the others so it goes through the same ownership rule; flattened
    # on output, where "overall" is one row per model.
    overall: dict[str, dict[str, Cell]] = {}
    models_out: dict[str, Any] = {}

    # Which run owns each (kind, key, model) cell: the newest run with *evidence*
    # for it (a scored row, or an unsafe one), falling back to the newest run that
    # covered it at all. "Newest that covered it" let a staging-outage run — every
    # row infra-excluded — take the cell and blank a model's scored history; an
    # all-excluded run is thin evidence and must not hide thicker, older evidence.
    # Runs are visited newest-first, so the first to claim keeps it. The overall
    # ("all") row goes through the same rule, so a model's overall figure comes from
    # its newest run that scored anything rather than its newest run.
    newest_covering: dict[tuple[str, str, str], str] = {}
    newest_evidence: dict[tuple[str, str, str], str] = {}
    for run in all_runs:
        for case in run.cases:
            evidence = _is_evidence(case)
            for claim in _claims(case, run.model):
                newest_covering.setdefault(claim, run.run_dir)
                if evidence:
                    newest_evidence.setdefault(claim, run.run_dir)
    owner = {
        claim: newest_evidence.get(claim, held) for claim, held in newest_covering.items()
    }

    def _cell_for(
        table: dict[str, dict[str, Cell]], kind: str, key: str, model: str, run: Run
    ) -> Cell | None:
        """The cell this run may write to, or None if another run owns it."""
        if owner.get((kind, key, model)) != run.run_dir:
            # Mixing another run's cases in would blend two states of the harness.
            return None
        return table.setdefault(key, {}).setdefault(model, Cell(run_dir=run.run_dir))

    for model, run in latest.items():
        entry = registry.get(model)
        models_out[model] = {
            **(
                entry.as_dict()
                if entry
                else {
                    "key": model,
                    "params_b": None,
                    "provider": "unknown",
                    "tool_filter_mode": None,
                    # A benchmarked model missing from the registry has no size, so
                    # the router cannot rank it. Say so rather than defaulting it
                    # to cloud-last and letting it quietly never be recommended.
                    "notes": "not in datasets/models.json — add it to make this "
                    "model eligible for routing recommendations",
                }
            ),
            "run_dir": run.run_dir,
            "timestamp": run.timestamp,
            "in_registry": entry is not None,
        }

    # Every run, newest first — not just each model's newest — so a model swept
    # domain by domain keeps all of its domains. Cell ownership (above) is what
    # stops an older run from being blended into a cell a newer one already covered.
    for run in all_runs:
        model = run.model
        if model not in models_out:
            # latest_run_per_model() covers every model with a run, so this is
            # unreachable — skip rather than inventing a metadata row for it.
            continue
        for case in run.cases:
            overall_cell = _cell_for(overall, "overall", "all", model, run)
            if overall_cell is not None:
                overall_cell.add(case)

            domain_cell = _cell_for(domains, "domain", _case_domain(case), model, run)
            if domain_cell is not None:
                domain_cell.add(case)

            for tool in _case_tools(case):
                tool_cell = _cell_for(tools, "tool", tool, model, run)
                if tool_cell is not None:
                    tool_cell.add(case)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "env_mode": lane,
        "pass_threshold": PASS_THRESHOLD,
        "models": models_out,
        "domain_pass_rate": DOMAIN_PASS_RATE,
        "tool_pass_rate": TOOL_PASS_RATE,
        "overall": {m: c.as_dict() for m, c in overall.get("all", {}).items()},
        "domains": {
            domain: {m: c.as_dict() for m, c in cells.items()}
            for domain, cells in sorted(domains.items())
        },
        "tools": {
            tool: {m: c.as_dict(TOOL_PASS_RATE) for m, c in cells.items()}
            for tool, cells in sorted(tools.items())
        },
    }


def _claims(case: dict, model: str) -> list[tuple[str, str, str]]:
    """Every (kind, key, model) cell this case would be added to."""
    return [
        ("overall", "all", model),
        ("domain", _case_domain(case), model),
        *(("tool", tool, model) for tool in _case_tools(case)),
    ]


UNCLASSIFIED = "unclassified"


def _case_domain(case: dict) -> str:
    """The routing domain for a persisted case record.

    Runs saved before domains existed have no field, so it is recovered from the
    dataset by case id — otherwise every historical run collapses into one
    meaningless bucket. Cases whose id is no longer in any dataset stay
    ``unclassified``, and the router skips that: a domain we can't name is not
    something we can recommend a model for.
    """
    domain = case.get("domain")
    if domain:
        return str(domain)
    return _dataset_domains().get(str(case.get("case_id", "")), UNCLASSIFIED)


_DOMAIN_CACHE: dict[str, str] | None = None


def _dataset_domains() -> dict[str, str]:
    global _DOMAIN_CACHE
    if _DOMAIN_CACHE is not None:
        return _DOMAIN_CACHE
    mapping: dict[str, str] = {}
    try:
        from bench.dataset import load_all_cases

        for case in load_all_cases():
            mapping[case.id] = case.domain
    except Exception:
        mapping = {}
    _DOMAIN_CACHE = mapping
    return mapping


def _case_tools(case: dict) -> list[str]:
    """Which tools this case is evidence about.

    The *expected* tools, not the called ones: a case is evidence that a model can
    (or cannot) use the tool it was supposed to use. Crediting a tool the model
    happened to call would let a wrong call improve that tool's score.
    """
    tool_case = case.get("case_id", "")
    expected = case.get("expected_tools")
    if expected is None:
        # Older result files didn't persist expectations; recover from the
        # dataset so historical runs still populate the tool matrix.
        expected = _dataset_expected_tools().get(tool_case, [])
    return sorted({normalize_tool_name(str(t)) for t in expected or []})


_EXPECTED_CACHE: dict[str, list[str]] | None = None


def _dataset_expected_tools() -> dict[str, list[str]]:
    global _EXPECTED_CACHE
    if _EXPECTED_CACHE is not None:
        return _EXPECTED_CACHE
    mapping: dict[str, list[str]] = {}
    try:
        from bench.dataset import load_all_cases

        for case in load_all_cases():
            mapping[case.id] = list(getattr(case, "expected_tools", []) or [])
    except Exception:
        mapping = {}
    _EXPECTED_CACHE = mapping
    return mapping


def matrix_path(lane: str = "live") -> Path:
    """Each lane keeps its own file, so a mock matrix can never stand in for live."""
    return RESULTS_DIR / ("matrix.json" if lane == "live" else f"matrix_{lane}.json")


def save_matrix(matrix: dict[str, Any], path: Path | None = None) -> Path:
    path = path or matrix_path(str(matrix.get("env_mode") or "live"))
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic: the dashboard and the router read this file while a sweep rewrites
    # it, and a torn write reads as "no matrix" (load_matrix swallows the error).
    atomic_write_text(path, json.dumps(matrix, indent=2) + "\n")
    return path


def load_matrix(path: Path | None = None, *, lane: str = "live") -> dict[str, Any] | None:
    path = path or matrix_path(lane)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    # A file written before lanes existed is live by construction.
    if (data.get("env_mode") or "live") != lane:
        return None
    return data
