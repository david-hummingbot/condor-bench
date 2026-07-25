"""bench-lite core: deterministic golden grading over prebuilt artifacts.

Every ready instance's OWN reference is backtested as a candidate and graded
against its frozen goldens — no LLM, no network. Expected: 100% resolved
with entry F1 = 1.0. A failure means one of three real problems:

- a reference was edited without `make goldens` (stale goldens);
- the condor-simple engine's semantics drifted (a golden-visible behavior
  change that must be reviewed and consciously re-frozen);
- the grader itself regressed.

Run: cd ~/condor-bench && make bench-lite
(uv run --project ~/condor-simple python harness/selfcheck.py)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BENCH_ROOT / "harness"))

from grade import grade_instance  # noqa: E402
from run_artifact import backtest_candidate  # noqa: E402


def main() -> int:
    instances_dir = BENCH_ROOT / "datasets" / "instances"
    fixtures_dir = BENCH_ROOT / "datasets" / "fixtures"
    failures = 0
    n_graded = 0
    for inst_dir in sorted(instances_dir.iterdir()):
        meta_path = inst_dir / "instance.json"
        if not meta_path.exists():
            continue
        instance = json.loads(meta_path.read_text())
        if instance.get("feasibility") != "expressible":
            continue
        ref = inst_dir / "reference" / "strategy.py"
        intents = backtest_candidate(ref, instance["id"])
        result = grade_instance(inst_dir, intents, fixtures_dir)
        n_graded += 1
        status = "ok" if result["resolved"] else "FAIL"
        detail = "; ".join(
            f"{f['fixture']}: f1={f['entry_f1']}"
            + (f" CRITICAL={f['critical_failures']}" if f["critical_failures"] else "")
            for f in result["fixtures"]
        )
        print(f"  {status:>4}  {instance['id']:<24} {detail}")
        if not result["resolved"]:
            failures += 1

    print(f"\nself-check: {n_graded - failures}/{n_graded} instances resolved")
    if failures:
        print(
            "A self-check failure means stale goldens (rerun `make goldens` and "
            "review the diff), engine-semantics drift, or a grader regression."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
