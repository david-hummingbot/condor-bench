"""Baseline store — records only latency from the reference model run.

The baseline is now used exclusively for latency normalisation. Quality scoring
is reference-free (judge evaluates each response on its own merits). Tool
accuracy is scored against dataset expected_tools ground truth.

Each record carries a fingerprint of the case it measured. Without one a record
is just a number with a case id on it, and an edited case keeps that number
silently: `tool_consult_001` went from a blocking `consult` to
`delegate(action="start")`, which returns the moment the task is handed off, and
kept a reference measured against the blocking call — and since latency scores
`min(1, baseline / test)`, every model since has collected a free 1.0 there.
`t002` went from one `manage_executors` call to three without a character of its
question changing. Neither showed up anywhere; both had to be found by diffing
the dataset against the commit the baselines predate.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import BASELINE_DIR, BASELINE_MODEL, case_timeout_s
from bench.cleanup import teardown
from bench.client import run_case
from bench.dataset import is_mutating
from bench.market_resolver import resolve_cases
from metrics.answer_quality import is_infra_failure


# What the fingerprint covers, and why.
#
# In: what decides how much work the case is — the text the model is handed, the
# turns it is handed, and the trajectory it is expected to walk. The trajectory
# is in here because `t002` changed from one `manage_executors` call to
# `list_executors` → `stop_executor` → journal without its question changing at
# all: `expected_tools` was the only place that showed.
#
# Out, deliberately:
#   * `expected_tool_params` / `expected_no_calls` — ground truth for scoring,
#     not input. Pinning `amount` does not make the model work harder, and a
#     fingerprint that fires on every scoring tweak is one people learn to ignore.
#   * the `markets` block and whatever it binds to — the same dataset binds to
#     different connectors on different boxes, so a latency reference can never
#     promise a venue and should not pretend to notice one changing.
#   * `config["agent_key"]` — a production-shaped fixture that bench overrides
#     with the model under test (see bench.client.build_tick_prompt_for_case).
_FINGERPRINT_FIELDS = (
    "question",
    "turns",
    "agent_slug",
    # A tick's prompt is assembled from these, so each one is part of its input.
    "scenario_name",
    "agent_instructions",
    "strategy_instructions",
    "core_data",
    "risk_state",
    "learnings",
    "summary",
    "recent_decisions",
    "tick_number",
)


def case_fingerprint(case: Any) -> str:
    """A short digest of the case as the baseline measured it.

    Short because it is read by humans in a report, not compared for security;
    twelve hex characters over this payload is far past any collision that would
    matter for 87 cases.
    """
    payload: dict[str, Any] = {"type": getattr(case, "type", "")}
    for name in _FINGERPRINT_FIELDS:
        value = getattr(case, name, None)
        if value is None or value == "" or value == [] or value == {}:
            continue
        payload[name] = value
    config = dict(getattr(case, "config", None) or {})
    config.pop("agent_key", None)
    if config:
        payload["config"] = config
    # `expected_tools` is a property on tick cases, returning expected_tool_calls.
    payload["expected_tools"] = sorted(getattr(case, "expected_tools", None) or [])
    blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


# A baseline is one of four things, and the difference decides what to do about it.
BASELINE_OK = "ok"
BASELINE_MISSING = "missing"
# Recorded before fingerprints existed. Not the same as stale: it may well still
# be accurate, there is simply no evidence either way — and inventing the
# evidence by stamping today's fingerprint onto a record measured months ago is
# exactly the provenance fiction this field exists to prevent.
BASELINE_UNVERIFIED = "unverified"
BASELINE_STALE = "stale"


def baseline_status(case: Any, record: "BaselineRecord | None") -> str:
    if record is None:
        return BASELINE_MISSING
    if not record.fingerprint:
        return BASELINE_UNVERIFIED
    return BASELINE_OK if record.fingerprint == case_fingerprint(case) else BASELINE_STALE


def classify_baselines(cases: list[Any], store: "BaselineStore") -> dict[str, list[str]]:
    """``{status: [case_id, …]}`` for every case given, statuses always present."""
    out: dict[str, list[str]] = {
        BASELINE_OK: [],
        BASELINE_STALE: [],
        BASELINE_UNVERIFIED: [],
        BASELINE_MISSING: [],
    }
    for case in cases:
        out[baseline_status(case, store.load(case.id))].append(case.id)
    return out


@dataclass
class BaselineRecord:
    case_id: str
    model: str
    latency_s: float
    timestamp: str = ""
    # Digest of the case this latency was measured against. Empty on records
    # written before fingerprints existed.
    fingerprint: str = ""

    def as_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "model": self.model,
            "latency_s": round(self.latency_s, 3),
            "timestamp": self.timestamp,
            "fingerprint": self.fingerprint,
        }


class BaselineStore:
    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory or BASELINE_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, case_id: str) -> Path:
        return self._dir / f"{case_id}.json"

    def save(self, record: BaselineRecord) -> None:
        self._path(record.case_id).write_text(json.dumps(record.as_dict(), indent=2))

    def load(self, case_id: str) -> BaselineRecord | None:
        p = self._path(case_id)
        if not p.exists():
            return None
        data = json.loads(p.read_text())
        return BaselineRecord(
            case_id=data["case_id"],
            model=data["model"],
            latency_s=float(data.get("latency_s", 30.0)),
            timestamp=data.get("timestamp", ""),
            fingerprint=str(data.get("fingerprint", "")),
        )

    def exists(self, case_id: str) -> bool:
        return self._path(case_id).exists()

    def list(self) -> list[str]:
        return [p.stem for p in sorted(self._dir.glob("*.json"))]

    def missing(self, case_ids: list[str]) -> list[str]:
        return [c for c in case_ids if not self.exists(c)]


async def generate_baselines(
    cases: list[Any],
    store: BaselineStore,
    model: str = BASELINE_MODEL,
    overwrite: bool = False,
    stale: bool = False,
    only: set[str] | None = None,
) -> None:
    """Run all cases with the baseline model and store latency records.

    ``stale`` adds the cases whose fingerprint no longer matches the record. It is
    opt-in rather than automatic because re-measuring is not free: a baseline run
    goes through the same path a scored run does, so it executes the mutating and
    destructive cases for real. Nobody should discover that by typing the command
    that used to be a no-op.

    ``only`` narrows the run to named case ids. Without it, re-measuring a handful
    of cases meant deleting their records so they counted as missing — which also
    deletes the reference the new ceiling scales from, dropping every one of them
    to the CASE_TIMEOUT_MIN_S floor and re-killing the slow case that needed the
    room. Keeping the record and naming the case is the only way to get both.
    """
    from rich.console import Console
    from rich.progress import track
    console = Console()

    def _wanted(case: Any) -> bool:
        if only is not None and case.id not in only:
            return False
        if overwrite or not store.exists(case.id):
            return True
        return stale and baseline_status(case, store.load(case.id)) == BASELINE_STALE

    to_run = [c for c in cases if _wanted(c)]
    if only:
        unknown = only - {c.id for c in cases}
        if unknown:
            console.print(
                f"[yellow]Not in the dataset, ignored: {', '.join(sorted(unknown))}[/yellow]"
            )
    if not to_run:
        unverified = classify_baselines(cases, store)[BASELINE_UNVERIFIED]
        console.print("[green]All baselines already exist.[/green]")
        if unverified:
            console.print(
                f"[dim]{len(unverified)} of them predate fingerprints, so whether "
                "they still describe their case is unknown — `baseline-check` "
                "lists them.[/dim]"
            )
        return

    console.print(f"Generating baselines for {len(to_run)} cases with [bold]{model}[/bold]")

    # Bind declared markets first, exactly as a scored run does. Seventeen cases
    # carry `{venue.connector}` / `{perp.pair}` placeholders, and this path used to
    # hand them to the model verbatim: `tool_create_position_executor_002` was asked
    # to open a position "on connector `{venue.connector}` for {venue.pair}", which
    # the reference model quite correctly declined to do. The recorded latency was
    # then 5.8s of a model reading an unanswerable question, standing in as the
    # reference for a case that opens a real position — the *rewarding* direction,
    # since latency scores min(1, baseline / test).
    #
    # This is the same rule the comment below states about the timeout, applied to
    # the other half of the wiring: a reference measured against a different prompt
    # than the runs it scores is not a reference.
    bound_by_id = {c.id: c for c in to_run}
    try:
        bound, resolutions = await resolve_cases(to_run)
        bound_by_id = {c.id: c for c in bound}
    except Exception as exc:  # network probe failed — say so, do not measure blind
        console.print(
            f"[red]Could not resolve declared markets ({exc}). Baselines for "
            "templated cases would be measured against literal placeholders, so "
            "nothing was recorded.[/red]"
        )
        return
    unbound = [cid for cid, r in resolutions.items() if not r.ok]
    if unbound:
        console.print(
            f"[yellow]{len(unbound)} case(s) could not bind their declared markets "
            f"and are skipped rather than measured against a placeholder: "
            f"{', '.join(sorted(unbound))}[/yellow]"
        )
        to_run = [c for c in to_run if c.id not in set(unbound)]

    for case in track(to_run, description="Baseline"):
        # The bound copy is what runs; the dataset case is what gets fingerprinted.
        # Binding is per-box — the same case takes binance here and hyperliquid
        # elsewhere — so a digest over the bound copy would call every baseline
        # stale on the next machine.
        runnable = bound_by_id.get(case.id, case)
        try:
            # Baselines are latency references, so they must be produced by the
            # same code path a test run uses — otherwise the reference is measured
            # against different wiring than the runs it scores. That now includes
            # the timeout: latency_score is min(1, baseline / test), so an inflated
            # baseline is *rewarding*, not penalising. `c012` once took 609s for a
            # bare manage_skill:list, and a reference that long would have handed
            # every model a free 1.0 on that case forever. Better to record no
            # baseline than a runaway one.
            #
            # Which is why the ceiling is `case_timeout_s`, the same per-case scale a
            # scored run gets, and not the flat CASE_TIMEOUT_S this used to pass — the
            # one number that comment says a run no longer uses. The flat 180s cut off
            # the cases it was least able to judge: agent_meteora_launch_lp_007 has a
            # 136.3s reference and was killed at 180s, where its own scale allows 545s.
            # Re-measuring scales from the record being replaced, since how long the
            # case has always taken is the best estimate of how long it will take;
            # a case with none falls to the CASE_TIMEOUT_MIN_S floor.
            prior = store.load(case.id)
            ceiling = case_timeout_s(prior.latency_s if prior else None)
            result = await asyncio.wait_for(
                run_case(runnable, model), timeout=ceiling
            )
        except asyncio.TimeoutError:
            console.print(
                f"[red]Timeout on {case.id} after {ceiling:.0f}s — "
                "no baseline recorded[/red]"
            )
            continue
        except Exception as exc:
            console.print(f"[red]Error on {case.id}: {exc}[/red]")
            continue

        # A run that failed measured the failure, not the job. `explore_dex_pools`
        # answering 404 because the gateway has no CLMM route, or `manage_amm`
        # refusing because the Solana wallet is still the literal string
        # `<solana-wallet-address>`, produces a tidy 4.9s that would then stand as
        # the reference for a case about reading pools. Same rule as the timeout
        # above: better to record no baseline than a wrong one.
        #
        # Checked the way the scorer checks it, and for the same reason it has to
        # be checked that way: `result.error` is None here. The client catches the
        # tool failure and yields it as *response text* — the run "succeeded" and
        # answered "(error: Tool 'explore_dex_pools' exceeded max retries count of
        # 1)". A guard on `.error` alone reads that as a clean 4.9s reference,
        # which is how five DEX baselines were recorded against a broken gateway.
        infra_blob = getattr(result, "response", "") or (getattr(result, "error", None) or "")
        if is_infra_failure(infra_blob):
            console.print(
                f"[yellow]{case.id}: {str(infra_blob).strip()[:100]} — no baseline "
                "recorded; a reference measured against a broken dependency is "
                "worse than none[/yellow]"
            )
            continue

        # Same teardown a scored run does. Baselining the whole dataset executes
        # every mutating and destructive case, so without this it leaves behind
        # executors, routines, strategies and leverage changes — and the pre-flight's
        # orphaned-executor check is blocking, so one baseline run would lock out
        # every run after it.
        if is_mutating(case):
            report = await teardown(
                result,
                model,
                agent_slug=getattr(runnable, "agent_slug", None),
                tick=getattr(case, "type", "") == "tick",
            )
            for row in report.kept_positions:
                console.print(
                    f"      [yellow]position kept: {row.get('tool')} "
                    f"{row.get('identifier')} — {row.get('note')}[/yellow]"
                )
            for row in report.failed + report.manual:
                console.print(
                    f"      [yellow]left behind: {row.get('tool')} "
                    f"{row.get('identifier')} — "
                    f"{row.get('error') or row.get('reason', 'manual')}[/yellow]"
                )

        ts = datetime.now(timezone.utc).isoformat()
        record = BaselineRecord(
            case_id=result.case_id,
            model=model,
            latency_s=result.latency_s,
            timestamp=ts,
            fingerprint=case_fingerprint(case),
        )
        store.save(record)
        console.print(f"  [dim]{case.id}[/dim] → {result.latency_s:.1f}s")
