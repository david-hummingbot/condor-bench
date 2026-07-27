"""Build instance.json + frozen goldens for curated botcamp instances.

For every `status: ready` entry in botcamp_curated.yaml this runs the
instance's reference strategy over each of its fixtures through the Condor
backtest engine and freezes the result:

    datasets/instances/<id>/instance.json             (generated — do not edit)
    datasets/instances/<id>/golden/<fixture>/intents.jsonl
    datasets/instances/<id>/golden/<fixture>/summary.json

`status: decline` entries get instance.json only (their golden is the decline
itself, captured in instance.json's decline_reason). `todo` entries are
skipped. Idempotent: goldens are overwritten from the current reference —
review the diff before committing, that diff IS a golden change.

Run under the condor-simple environment:  cd ~/condor-bench && make goldens
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

BENCH_ROOT = Path(__file__).resolve().parent.parent
INSTANCES_DIR = BENCH_ROOT / "datasets" / "instances"
FIXTURES_DIR = BENCH_ROOT / "datasets" / "fixtures"


def build_ready(entry: dict, defaults: dict, problems: list[str]) -> dict | None:
    from condor.backtest import Backtest, load_fixture, load_strategy

    inst_dir = INSTANCES_DIR / entry["id"]
    ref = inst_dir / "reference" / "strategy.py"
    if not (inst_dir / "prompt.md").exists():
        problems.append(f"{entry['id']}: missing prompt.md")
        return None
    if not ref.exists():
        problems.append(f"{entry['id']}: status=ready but no reference/strategy.py")
        return None

    strat = load_strategy(ref)
    golden_stats = {}
    for fixture_id in entry["fixtures"]:
        fdir = FIXTURES_DIR / fixture_id
        if not (fdir / "meta.json").exists():
            problems.append(f"{entry['id']}: fixture {fixture_id} not recorded")
            continue
        fx = load_fixture(fdir)
        result = Backtest.from_fixture(
            fx, strat.decide, params=strat.params, warmup=int(defaults.get("warmup", 50))
        ).run()
        out = inst_dir / "golden" / fixture_id
        out.mkdir(parents=True, exist_ok=True)
        intents = result.executor_intents()
        (out / "intents.jsonl").write_text(
            "".join(json.dumps(i, default=str) + "\n" for i in intents)
        )
        (out / "summary.json").write_text(
            json.dumps(result.summary, indent=2, default=str) + "\n"
        )
        golden_stats[fixture_id] = {
            "intents": len(intents),
            "net_pnl_quote": round(result.summary["net_pnl_quote"], 2),
            "close_types": result.summary["close_types"],
        }
    return {"params": strat.params, "golden": golden_stats}


def main() -> int:
    spec = yaml.safe_load((BENCH_ROOT / "collect" / "botcamp_curated.yaml").read_text())
    defaults = spec.get("defaults", {})
    problems: list[str] = []
    counts = {"ready": 0, "decline": 0, "todo": 0}

    for entry in spec["instances"]:
        status = entry["status"]
        counts[status] = counts.get(status, 0) + 1
        if status == "todo":
            print(f"  {entry['id']}: todo — skipped ({entry.get('notes', '')[:60]})")
            continue

        inst_dir = INSTANCES_DIR / entry["id"]
        inst_dir.mkdir(parents=True, exist_ok=True)
        feasibility = {
            "decline": "needs-decline",
            "clarify": "clarification-required",
        }.get(status, "expressible")
        instance = {
            "id": entry["id"],
            "source": entry.get("source", "botcamp"),
            "botcamp_name": entry["botcamp_name"],
            "category": entry["category"],
            "difficulty": entry["difficulty"],
            "feasibility": feasibility,
        }

        if status in ("decline", "clarify"):
            if not (inst_dir / "prompt.md").exists():
                problems.append(f"{entry['id']}: missing prompt.md")
            if status == "decline":
                instance["decline_reason"] = entry["decline_reason"].strip()
            else:
                instance["expected_clarifications"] = entry["expected_clarifications"]
            print(f"  {entry['id']}: {status} instance")
        else:
            built = build_ready(entry, defaults, problems)
            if built is None:
                continue
            instance["fixtures"] = entry["fixtures"]
            instance.update(built)
            for fixture_id, s in built["golden"].items():
                print(
                    f"  {entry['id']} @ {fixture_id}: {s['intents']} intents, "
                    f"pnl {s['net_pnl_quote']:+}, {s['close_types']}"
                )

        (inst_dir / "instance.json").write_text(
            json.dumps(instance, indent=2, default=str) + "\n"
        )

    print(f"\ninstances: {counts}")
    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print(f"  - {p}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
