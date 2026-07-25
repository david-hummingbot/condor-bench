"""Deterministic grader: candidate executor intents vs frozen goldens.

Stdlib only — importable from any environment. Grades one candidate intent
timeline (produced by backtesting a generated strategy on the same fixture
the golden was frozen from) against the golden per addendum §9:

- entry precision / recall / F1 (events matched by (type, side) within a
  timing tolerance);
- exit match rate and close-type agreement (diagnostic);
- sizing fidelity per matched pair (catches lost asymmetric sizing);
- barrier fidelity (a golden-set barrier missing or far off is critical);
- resolved = entry F1 >= threshold AND no critical mismatch.

Scores never hide critical field errors: `resolved` is false on any critical
failure regardless of F1, and every failure is named in `critical_failures`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

INTERVAL_S = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "8h": 28800, "12h": 43200,
    "1d": 86400,
}

_BARRIER_FIELDS = (
    "take_profit_pct",
    "stop_loss_pct",
    "trailing_activation_pct",
    "trailing_delta_pct",
    "time_limit_s",
)

# Golden close types that reflect strategy-driven exits worth timing-matching.
_TIMED_EXITS = {"early_stop", "take_profit", "stop_loss", "trailing_stop", "time_limit"}


@dataclass
class GradeOptions:
    tolerance_candles: int = 2
    notional_rel_tol: float = 0.10
    barrier_rel_tol: float = 0.25
    entry_f1_threshold: float = 0.90


@dataclass
class FixtureGrade:
    fixture: str
    n_golden: int
    n_candidate: int
    n_matched: int
    entry_precision: float
    entry_recall: float
    entry_f1: float
    exit_match_rate: float | None  # None when the golden has no timed exits
    close_type_agreement: float | None
    max_sizing_rel_err: float | None
    critical_failures: list[str] = field(default_factory=list)
    resolved: bool = False

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def load_intents(path: str | Path) -> list[dict]:
    out = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _match_entries(golden: list[dict], candidate: list[dict], tol_s: float):
    """Greedy nearest-in-time matching within (type, side) groups."""
    matches: list[tuple[int, int]] = []
    used: set[int] = set()
    for gi, g in sorted(enumerate(golden), key=lambda x: x[1]["created_ts"]):
        best, best_dt = None, None
        for ci, c in enumerate(candidate):
            if ci in used:
                continue
            if c.get("type") != g.get("type") or c.get("side") != g.get("side"):
                continue
            dt = abs(float(c["created_ts"]) - float(g["created_ts"]))
            if dt <= tol_s and (best_dt is None or dt < best_dt):
                best, best_dt = ci, dt
        if best is not None:
            used.add(best)
            matches.append((gi, best))
    return matches


def _rel_err(candidate_v: float, golden_v: float) -> float:
    if golden_v == 0:
        return 0.0 if candidate_v == 0 else float("inf")
    return abs(candidate_v - golden_v) / abs(golden_v)


def grade_fixture(
    fixture: str,
    golden: list[dict],
    candidate: list[dict],
    interval_s: float,
    opts: GradeOptions | None = None,
) -> FixtureGrade:
    opts = opts or GradeOptions()
    tol_s = opts.tolerance_candles * interval_s
    matches = _match_entries(golden, candidate, tol_s)

    n_g, n_c, n_m = len(golden), len(candidate), len(matches)
    precision = n_m / n_c if n_c else (1.0 if n_g == 0 else 0.0)
    recall = n_m / n_g if n_g else (1.0 if n_c == 0 else 0.0)
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    critical: list[str] = []
    sizing_errs: list[float] = []
    exit_hits, exit_total = 0, 0
    ct_hits, ct_total = 0, 0

    for gi, ci in matches:
        g, c = golden[gi], candidate[ci]
        sizing_errs.append(_rel_err(float(c["notional_quote"]), float(g["notional_quote"])))
        for fname in _BARRIER_FIELDS:
            gv, cv = g.get(fname), c.get(fname)
            if gv is None:
                continue  # candidate adding extra protection is not a failure
            if cv is None:
                critical.append(f"missing_{fname}")
            elif _rel_err(float(cv), float(gv)) > opts.barrier_rel_tol:
                critical.append(f"{fname}_off")
        if g.get("close_type") in _TIMED_EXITS:
            exit_total += 1
            if c.get("close_ts") is not None and g.get("close_ts") is not None:
                if abs(float(c["close_ts"]) - float(g["close_ts"])) <= tol_s:
                    exit_hits += 1
        if g.get("close_type") is not None:
            ct_total += 1
            if c.get("close_type") == g.get("close_type"):
                ct_hits += 1

    max_sizing = max(sizing_errs) if sizing_errs else None
    if max_sizing is not None and max_sizing > opts.notional_rel_tol:
        critical.append("sizing_off")
    critical = sorted(set(critical))

    resolved = f1 >= opts.entry_f1_threshold and not critical
    return FixtureGrade(
        fixture=fixture,
        n_golden=n_g,
        n_candidate=n_c,
        n_matched=n_m,
        entry_precision=round(precision, 4),
        entry_recall=round(recall, 4),
        entry_f1=round(f1, 4),
        exit_match_rate=round(exit_hits / exit_total, 4) if exit_total else None,
        close_type_agreement=round(ct_hits / ct_total, 4) if ct_total else None,
        max_sizing_rel_err=round(max_sizing, 4) if max_sizing is not None else None,
        critical_failures=critical,
        resolved=resolved,
    )


def grade_instance(
    instance_dir: str | Path,
    candidate_intents: dict[str, list[dict]],
    fixtures_dir: str | Path,
    opts: GradeOptions | None = None,
) -> dict:
    """Grade a candidate across every golden fixture of one instance.

    ``candidate_intents`` maps fixture_id -> intent rows (from backtesting
    the candidate artifact on that fixture). Instance resolved = every
    fixture resolved.
    """
    instance_dir = Path(instance_dir)
    instance = json.loads((instance_dir / "instance.json").read_text())
    grades: list[FixtureGrade] = []
    for fixture_id in instance.get("fixtures", []):
        meta = json.loads(
            (Path(fixtures_dir) / fixture_id / "meta.json").read_text()
        )
        golden = load_intents(instance_dir / "golden" / fixture_id / "intents.jsonl")
        cand = candidate_intents.get(fixture_id)
        if cand is None:
            grades.append(
                FixtureGrade(
                    fixture=fixture_id, n_golden=len(golden), n_candidate=0,
                    n_matched=0, entry_precision=0.0, entry_recall=0.0,
                    entry_f1=0.0, exit_match_rate=None, close_type_agreement=None,
                    max_sizing_rel_err=None,
                    critical_failures=["no_candidate_backtest"], resolved=False,
                )
            )
            continue
        grades.append(
            grade_fixture(fixture_id, golden, cand, INTERVAL_S[meta["interval"]], opts)
        )
    return {
        "instance": instance["id"],
        "resolved": bool(grades) and all(g.resolved for g in grades),
        "fixtures": [g.to_dict() for g in grades],
    }
