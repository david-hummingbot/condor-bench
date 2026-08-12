"""Turn a benchmark matrix into model-routing recommendations for Condor.

The question this answers: *what is the smallest model that can do each job?*

Algorithm, per domain:

1. Consider only models in ``datasets/models.json`` — a model with no recorded
   parameter count cannot be ranked by size, so recommending it would be guessing.
2. Sort ascending by ``params_b``; cloud models (no parameter count) sort last, so
   a local model that passes always beats a cloud one that also passes.
3. Take the first model that **passes the domain**:
   - pass rate ≥ ``--min-pass-rate`` (default 0.80), and
   - no ``destructive`` case scored below ``DESTRUCTIVE_FLOOR``, and
   - at least ``min_cases`` cases actually scored — a 1-for-1 domain is not
     evidence, it's a coin flip that happened to land.
4. If nothing passes, recommend the best available model and mark the domain
   ``unmet``, naming the gap. A silent omission would read as "no opinion" when the
   real finding is "no model we tried is good enough here".

Token cost is **not a gate**. A 3B model that passes using three times the tokens
of a 7B model is still the recommendation by default — the study is about the
smallest model that works, and cost is a separate axis. ``prefer_lower_tokens``
opts into using tokens as a tie-break among models that are *equally small*, and
even then it can only reorder ties, never reject a passing model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import (
    DESTRUCTIVE_FLOOR,
    DOMAIN_PASS_RATE,
    MIN_TOOL_CASES,
    RESULTS_DIR,
    TOOL_PASS_RATE,
)
from bench.dataset import expert_agents, is_routing_domain, routing_domain_for
from bench.matrix import UNCLASSIFIED, ModelEntry, build_matrix, load_models

# Domains whose recommendation maps onto a concrete condor config key. Domains
# absent from this map still get a recommendation; they just don't produce a config
# line, because bench doesn't know where they'd be applied.
#
# Derived from datasets/agent_roles.json rather than hand-maintained, so the roster
# and the config keys cannot disagree — the previous literal dict is how three agents
# shipped upstream with no routing domain, and how a fourth kept a key after being
# reclassified as a strategy. Strategies are absent by construction: they inherit
# their base's model assignment.
#
# tick_execution is the one entry with no agent behind it: condor's agents/_defaults
# is a fallback model setting for agent ticks, not an agent with an AGENT.md.
_TICK_CONFIG_KEY = "agents/_defaults/agent_key"


def condor_config_keys() -> dict[str, str]:
    """domain -> condor config path, for every expert plus tick_execution."""
    keys = {
        routing_domain_for(slug): f"agents/{slug}/agent_key"
        for slug in sorted(expert_agents())
    }
    keys["tick_execution"] = _TICK_CONFIG_KEY
    return keys


CONDOR_CONFIG_KEYS = condor_config_keys()


@dataclass
class Candidate:
    model: str
    entry: ModelEntry
    pass_rate: float | None
    avg_composite: float | None
    scored: int
    excluded: int
    avg_total_tokens: float | None
    avg_cost_usd: float | None
    avg_latency_s: float | None
    destructive_failures: list[str] = field(default_factory=list)
    run_dir: str = ""

    def blockers(self, *, min_pass_rate: float, min_cases: int) -> list[str]:
        """Why this model can't be recommended for the domain. Empty means it can."""
        reasons: list[str] = []
        if self.scored < min_cases:
            reasons.append(
                f"only {self.scored} scored case(s) — needs {min_cases} to be evidence"
                + (f" ({self.excluded} excluded)" if self.excluded else "")
            )
        if self.pass_rate is None:
            reasons.append("no pass rate (nothing scorable)")
        elif self.pass_rate < min_pass_rate:
            reasons.append(f"pass rate {self.pass_rate:.0%} < {min_pass_rate:.0%}")
        if self.destructive_failures:
            reasons.append(
                "destructive case(s) below the "
                f"{DESTRUCTIVE_FLOOR:.2f} floor: {', '.join(self.destructive_failures)}"
            )
        return reasons

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "params_b": self.entry.params_b,
            "provider": self.entry.provider,
            "pass_rate": self.pass_rate,
            "avg_composite": self.avg_composite,
            "scored": self.scored,
            "excluded": self.excluded,
            "avg_total_tokens": self.avg_total_tokens,
            "avg_cost_usd": self.avg_cost_usd,
            "avg_latency_s": self.avg_latency_s,
            "run_dir": self.run_dir,
        }


def _candidates(
    cells: dict[str, dict], registry: dict[str, ModelEntry]
) -> list[Candidate]:
    out = []
    for model, cell in cells.items():
        entry = registry.get(model)
        if entry is None:
            # Unranked models are reported separately (see `unranked_models`) so
            # the omission is visible rather than looking like a missing run.
            continue
        out.append(
            Candidate(
                model=model,
                entry=entry,
                pass_rate=cell.get("pass_rate"),
                avg_composite=cell.get("avg_composite"),
                scored=int(cell.get("scored") or 0),
                excluded=int(cell.get("excluded") or 0),
                avg_total_tokens=cell.get("avg_total_tokens"),
                avg_cost_usd=cell.get("avg_cost_usd"),
                avg_latency_s=cell.get("avg_latency_s"),
                destructive_failures=list(cell.get("destructive_failures") or []),
                run_dir=str(cell.get("run_dir") or ""),
            )
        )
    return sorted(out, key=lambda c: c.entry.sort_key)


def _tie_break(passing: list[Candidate], prefer_lower_tokens: bool) -> tuple[Candidate, str | None]:
    """Pick among passing candidates. Returns (winner, tie_breaker_used)."""
    smallest = passing[0]
    if not prefer_lower_tokens:
        return smallest, None

    # Only models of the *same* size are tied. Preferring a larger model because
    # it used fewer tokens would abandon the whole premise of the study.
    tied = [c for c in passing if c.entry.sort_key[:2] == smallest.entry.sort_key[:2]]
    if len(tied) < 2:
        return smallest, None

    measured = [c for c in tied if c.avg_total_tokens is not None]
    if len(measured) < 2:
        # Unmeasured token counts are not zero; a model that reported nothing
        # cannot win a token tie-break.
        return smallest, None

    winner = min(measured, key=lambda c: (c.avg_total_tokens, c.model))
    if winner.model == smallest.model:
        return smallest, None
    return winner, (
        f"lower avg tokens among {len(measured)} models of the same size "
        f"({winner.avg_total_tokens:.0f} vs {smallest.avg_total_tokens:.0f})"
    )


def recommend(
    matrix: dict[str, Any],
    *,
    min_pass_rate: float = DOMAIN_PASS_RATE,
    min_cases: int = 3,
    min_tool_pass_rate: float = TOOL_PASS_RATE,
    min_tool_cases: int = MIN_TOOL_CASES,
    prefer_lower_tokens: bool = False,
    models_path: Path | None = None,
) -> dict[str, Any]:
    """Build routing recommendations from a matrix produced by ``bench.matrix``."""
    registry = {m.key: m for m in load_models(models_path)}
    benchmarked = set(matrix.get("models", {}))
    unranked = sorted(benchmarked - set(registry))

    recommendations: dict[str, Any] = {}
    unmet: dict[str, Any] = {}
    stale: dict[str, Any] = {}
    live_domains = _dataset_domains()

    for domain, cells in (matrix.get("domains") or {}).items():
        # Layer 2 buckets ("tool:market_data") are capabilities, not routing
        # targets — there is no Condor config key for them, and their per-tool
        # verdicts come out of `tool_gaps` where a single case is a fair sample.
        # "unclassified" holds cases whose dataset entry no longer exists, so
        # there is nothing to route either.
        if not is_routing_domain(domain) or domain == UNCLASSIFIED:
            continue

        # A domain no current dataset produces exists only in older results —
        # `routine_builder`, say, after condor deleted that agent. Reporting it as
        # "unmet" would read as a gap to close by benchmarking harder, when the
        # honest statement is that there is nothing left to route.
        if live_domains and domain not in live_domains:
            cells_with_data = {m: c for m, c in cells.items() if c.get("scored")}
            stale[domain] = {
                "reason": "no case in the current datasets produces this domain — "
                "the results are historical",
                "models_with_results": sorted(cells_with_data),
            }
            continue

        candidates = _candidates(cells, registry)
        if not candidates:
            unmet[domain] = {
                "reason": "no benchmarked model in datasets/models.json covers this domain",
                "candidates": [],
            }
            continue

        passing = [
            c
            for c in candidates
            if not c.blockers(min_pass_rate=min_pass_rate, min_cases=min_cases)
        ]

        if passing:
            winner, tie_breaker = _tie_break(passing, prefer_lower_tokens)
            local_passed = any(c.entry.provider != "cloud" for c in passing)
            recommendations[domain] = {
                **winner.as_dict(),
                "rationale": (
                    "Smallest passing model"
                    if tie_breaker is None
                    else "Smallest passing model, tie-broken on tokens"
                ),
                "tie_breaker": tie_breaker,
                "cloud_fallback": winner.entry.provider == "cloud",
                # A cloud recommendation with no local alternative is a finding
                # worth surfacing, not just a value.
                "no_local_passed": not local_passed,
                "config_key": CONDOR_CONFIG_KEYS.get(domain),
                "alternatives": [c.as_dict() for c in passing if c.model != winner.model],
            }
            continue

        # Nothing passed. Name the best attempt and exactly why it fell short, so
        # this reads as a measured gap rather than missing data.
        best = max(
            candidates,
            key=lambda c: (
                c.pass_rate if c.pass_rate is not None else -1.0,
                c.avg_composite if c.avg_composite is not None else -1.0,
            ),
        )
        blockers = {
            c.model: c.blockers(min_pass_rate=min_pass_rate, min_cases=min_cases)
            for c in candidates
        }
        unmet[domain] = {
            # Distinguish "the models aren't good enough" from "this domain has too
            # few cases to say" — the second is a dataset gap, not a model finding,
            # and reporting a 100%-scoring model as a failure would be nonsense.
            "reason": (
                f"only {best.scored} scored case(s) in this domain — needs "
                f"{min_cases} before a verdict counts as evidence"
                if all(
                    any("scored case" in b for b in reasons)
                    for reasons in blockers.values()
                )
                else "no benchmarked model met the pass criteria"
            ),
            "insufficient_evidence": all(
                any("scored case" in b for b in reasons) for reasons in blockers.values()
            ),
            "best_attempt": best.as_dict(),
            "blockers": blockers,
            "config_key": CONDOR_CONFIG_KEYS.get(domain),
        }

    config_snippet, config_conflicts = _config_snippet(recommendations)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "matrix_generated_at": matrix.get("generated_at"),
        "criteria": {
            "min_pass_rate": min_pass_rate,
            "min_cases": min_cases,
            "destructive_floor": DESTRUCTIVE_FLOOR,
            "pass_threshold": matrix.get("pass_threshold"),
            # The tool axis is judged on its own, looser bar. Carried here so a
            # reader of routing.json can never mistake a 67% tool row for a
            # domain row that scraped past 80%.
            "min_tool_pass_rate": min_tool_pass_rate,
            "min_tool_cases": min_tool_cases,
        },
        "routing_options": {"prefer_lower_tokens": prefer_lower_tokens},
        "recommendations": recommendations,
        "unmet_domains": unmet,
        "stale_domains": stale,
        "unranked_models": unranked,
        "unranked_note": (
            "These models were benchmarked but are absent from datasets/models.json, "
            "so they have no parameter count and cannot be ranked by size. Add them "
            "to make them eligible."
            if unranked
            else ""
        ),
        "condor_config_snippet": config_snippet,
        "config_conflicts": config_conflicts,
        "tool_gaps": _tool_gaps(
            matrix, registry, min_tool_pass_rate, min_tool_cases
        ),
    }


def _dataset_domains() -> set[str]:
    """Routing domains the current datasets actually produce.

    Empty when the datasets can't be read, which makes the staleness check a no-op
    rather than declaring every domain stale.
    """
    try:
        from bench.dataset import load_all_cases

        return {c.domain for c in load_all_cases() if is_routing_domain(c.domain)}
    except Exception:
        return set()


def _config_snippet(
    recommendations: dict[str, Any]
) -> tuple[dict[str, str], dict[str, Any]]:
    """Condor config lines, plus any key two domains disagree about.

    Several domains can share one config key — ``strategy_creation`` and
    ``market_making_expert`` are both that agent's ``agent_key``. When they
    recommend different models, one would silently overwrite the other in a plain
    dict. Take the larger model (the one that satisfies both domains) and report
    the disagreement rather than letting dict ordering decide.
    """
    by_key: dict[str, list[tuple[str, dict]]] = {}
    for domain, rec in recommendations.items():
        key = rec.get("config_key")
        if key:
            by_key.setdefault(key, []).append((domain, rec))

    snippet: dict[str, str] = {}
    conflicts: dict[str, Any] = {}
    for key, entries in by_key.items():
        models = {rec["model"] for _, rec in entries}
        if len(models) == 1:
            snippet[key] = entries[0][1]["model"]
            continue
        # Params None (cloud) sorts last and wins, which is the safe direction:
        # a shared key must satisfy the most demanding domain that uses it.
        winner_domain, winner = max(
            entries,
            key=lambda e: (
                e[1]["params_b"] if e[1].get("params_b") is not None else float("inf")
            ),
        )
        snippet[key] = winner["model"]
        conflicts[key] = {
            "chosen": winner["model"],
            "chosen_for": winner_domain,
            "reason": "shared config key — took the model that satisfies every domain using it",
            "per_domain": {domain: rec["model"] for domain, rec in entries},
        }
    return snippet, conflicts


def _tool_gaps(
    matrix: dict[str, Any],
    registry: dict[str, ModelEntry],
    min_tool_pass_rate: float = TOOL_PASS_RATE,
    min_tool_cases: int = MIN_TOOL_CASES,
) -> dict[str, Any]:
    """Per-tool smallest passing model, the tools nothing handles, and thin rows.

    Finer grained than the domain view and useful for a different decision: a
    domain can pass overall while one tool inside it fails for every local model,
    which is the signal to keep that specific capability on a cloud model.

    **This axis uses its own bar, not the domain one.** ``min_tool_pass_rate``
    (0.65) asks "can the model drive this tool at all", where the domain bar (0.80)
    asks "can it own this job". Reusing 0.80 here made every affordable sample size
    require a perfect score, so one unlucky case marked a tool unhandled — see the
    note on ``TOOL_PASS_RATE``.

    A model under ``min_tool_cases`` lands in ``thin`` rather than in
    ``smallest_passing`` or ``unhandled``. Reporting one case as a verdict is what
    let a single flake decide a tool's fate.
    """
    smallest: dict[str, Any] = {}
    unhandled: list[str] = []
    thin: dict[str, Any] = {}
    for tool, cells in (matrix.get("tools") or {}).items():
        candidates = _candidates(cells, registry)
        evidenced = [c for c in candidates if c.scored >= min_tool_cases]
        passing = [
            c
            for c in evidenced
            if c.pass_rate is not None
            and c.pass_rate >= min_tool_pass_rate
            and not c.destructive_failures
        ]
        if passing:
            winner = passing[0]
            smallest[tool] = {
                "model": winner.model,
                "params_b": winner.entry.params_b,
                "pass_rate": winner.pass_rate,
                "scored": winner.scored,
            }
        elif evidenced:
            unhandled.append(tool)
        elif candidates:
            # Measured, but not enough per model to call it either way.
            best = max(candidates, key=lambda c: c.scored)
            thin[tool] = {
                "best_scored": best.scored,
                "needs": min_tool_cases,
                "models_measured": len(candidates),
            }
    return {
        "smallest_passing": smallest,
        "unhandled": sorted(unhandled),
        "thin": thin,
        "criteria": {
            "min_tool_pass_rate": min_tool_pass_rate,
            "min_tool_cases": min_tool_cases,
        },
    }


def generate(
    *,
    min_pass_rate: float = DOMAIN_PASS_RATE,
    min_cases: int = 3,
    min_tool_pass_rate: float = TOOL_PASS_RATE,
    min_tool_cases: int = MIN_TOOL_CASES,
    prefer_lower_tokens: bool = False,
    results_dir: Path | None = None,
    models_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the matrix and the recommendations in one call. Returns (matrix, routing)."""
    matrix = build_matrix(results_dir=results_dir, models_path=models_path)
    routing = recommend(
        matrix,
        min_pass_rate=min_pass_rate,
        min_cases=min_cases,
        min_tool_pass_rate=min_tool_pass_rate,
        min_tool_cases=min_tool_cases,
        prefer_lower_tokens=prefer_lower_tokens,
        models_path=models_path,
    )
    return matrix, routing


def save_routing(routing: dict[str, Any], path: Path | None = None) -> Path:
    path = path or RESULTS_DIR / "routing_recommendations.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(routing, indent=2) + "\n")
    return path


def load_routing(path: Path | None = None) -> dict[str, Any] | None:
    path = path or RESULTS_DIR / "routing_recommendations.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None
