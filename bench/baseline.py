"""Baseline store — records only latency from the reference model run.

The baseline is now used exclusively for latency normalisation. Quality scoring
is reference-free (judge evaluates each response on its own merits). Tool
accuracy is scored against dataset expected_tools ground truth.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import BASELINE_DIR, BASELINE_MODEL
from bench.client import BenchmarkResult, run_consult, run_tick, build_tick_prompt_for_case


@dataclass
class BaselineRecord:
    case_id: str
    model: str
    latency_s: float
    timestamp: str = ""

    def as_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "model": self.model,
            "latency_s": round(self.latency_s, 3),
            "timestamp": self.timestamp,
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
) -> None:
    """Run all cases with the baseline model and store latency records."""
    from rich.console import Console
    from rich.progress import track
    console = Console()

    to_run = [c for c in cases if overwrite or not store.exists(c.id)]
    if not to_run:
        console.print("[green]All baselines already exist.[/green]")
        return

    console.print(f"Generating baselines for {len(to_run)} cases with [bold]{model}[/bold]")
    for case in track(to_run, description="Baseline"):
        try:
            if case.type == "tick":
                prompt = build_tick_prompt_for_case(case, model)
                result = await run_tick(case.id, prompt, model, case.mock_tools)
            else:
                result = await run_consult(
                    case.id, case.question, model,
                    extra_turns=getattr(case, "turns", None),
                    mock_tools=getattr(case, "mock_tools", None),
                )
        except Exception as exc:
            console.print(f"[red]Error on {case.id}: {exc}[/red]")
            continue

        ts = datetime.now(timezone.utc).isoformat()
        record = BaselineRecord(
            case_id=result.case_id,
            model=model,
            latency_s=result.latency_s,
            timestamp=ts,
        )
        store.save(record)
        console.print(f"  [dim]{case.id}[/dim] → {result.latency_s:.1f}s")
