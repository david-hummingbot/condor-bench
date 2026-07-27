"""Backtest a candidate strategy artifact over an instance's golden fixtures.

Runs under the condor-simple environment (owns the engine):

    uv run --project ~/condor-simple python harness/run_artifact.py \
        <strategy.py> <instance-id> [--out intents.json]

Emits {"<fixture_id>": [intent rows...]} — the input `grade.grade_instance`
expects. Library use: `backtest_candidate(...)`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BENCH_ROOT = Path(__file__).resolve().parent.parent


def backtest_candidate(
    strategy_path: str | Path, instance_id: str, warmup: int = 50
) -> dict[str, list[dict]]:
    from condor.backtest import Backtest, load_fixture, load_strategy

    instance = json.loads(
        (BENCH_ROOT / "datasets" / "instances" / instance_id / "instance.json").read_text()
    )
    strat = load_strategy(strategy_path)
    out: dict[str, list[dict]] = {}
    for fixture_id in instance.get("fixtures", []):
        fx = load_fixture(BENCH_ROOT / "datasets" / "fixtures" / fixture_id)
        result = Backtest.from_fixture(
            fx, strat.decide, params=strat.params, warmup=warmup
        ).run()
        out[fixture_id] = result.executor_intents()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("strategy")
    ap.add_argument("instance_id")
    ap.add_argument("--warmup", type=int, default=50)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    intents = backtest_candidate(args.strategy, args.instance_id, warmup=args.warmup)
    payload = json.dumps(intents, default=str)
    if args.out:
        Path(args.out).write_text(payload)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
