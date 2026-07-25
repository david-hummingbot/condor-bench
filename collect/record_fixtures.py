"""Record the regime fixtures declared in botcamp_curated.yaml.

Run under the condor-simple environment (it owns the engine + venue client):

    cd ~/condor-bench && make fixtures
    # == uv run --project ~/condor-simple python collect/record_fixtures.py

Existing fixture dirs are left untouched (fixtures are FROZEN once recorded —
delete a dir deliberately to re-record it). Note Hyperliquid's candleSnapshot
only reaches back ~7 months at 1h: record windows while they are reachable.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

BENCH_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = BENCH_ROOT / "datasets" / "fixtures"


def _ms(date_str: str) -> int:
    dt = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def main() -> int:
    from condor.backtest import record_fixture

    spec = yaml.safe_load((BENCH_ROOT / "collect" / "botcamp_curated.yaml").read_text())
    failures = 0
    for fixture_id, fx in spec["fixtures"].items():
        out_dir = FIXTURES_DIR / fixture_id
        if (out_dir / "meta.json").exists():
            print(f"  {fixture_id}: exists, skipping (delete the dir to re-record)")
            continue
        start_ms, end_ms = _ms(fx["start"]), _ms(fx["end"])
        days = (end_ms - start_ms) / 86_400_000
        try:
            record_fixture(
                fx["coin"], fx["interval"], days=days, out_dir=out_dir, end_ms=end_ms
            )
            print(f"  {fixture_id}: recorded {fx['coin']} {fx['interval']} {fx['start']}..{fx['end']}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  {fixture_id}: FAILED — {e}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
