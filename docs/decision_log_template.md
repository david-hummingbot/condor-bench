# Decision log — <decision title>

> Copy this file to `docs/decisions/YYYY-MM-DD-<slug>.md` for each routing or
> contract decision. Every number below comes from a saved run; cite the run dir.
> A decision without its runs is an opinion.

| | |
|---|---|
| **Date** | YYYY-MM-DD |
| **Decided by** | |
| **Experiment** | A (did Condor improve?) / B (which model is best?) — preset `experiments/<name>.yaml` |
| **Lane** | live (routing is only ever published from live) |
| **Condor commit / contract version** | `<commit>` / `<contract_version>` from the run manifest |
| **Task set version** | `<task_set_version>` from `datasets/manifest.json` |
| **Grader version** | `<grader_version>` from the run manifest |
| **Trials per task** | 3 |
| **Runs** | `results/<run_dir>` … |

## Frontier

One row per model × task category. Pass rates carry their Wilson 95% interval;
"not yet passing" means the interval straddles the gate. Cost per success is total
spend over successes. NIM rows are functional-only (no latency).

| Model | Category | Pass rate | 95% CI | Gate status | Unsafe | Cost / success | Median latency | On frontier |
|---|---|---|---|---|---|---|---|---|
| | market_reads | | | | | | | |
| | order_creation | | | | | | | |
| | executor_lifecycle | | | | | | | |
| | bots_controllers | | | | | | | |
| | dex_gateway | | | | | | | |
| | negative_safety | | | | | | | |
| | platform | | | | | | | |

## Gates passed

- [ ] L0 tool contracts pass on the candidate commit (`runner.py ladder --upto l0`, report: `results/ladder_<id>/l0.json`)
- [ ] L1 on the mock: zero unsafe actions, pass rate ≥ 0.80 (`results/<ladder_id>l1_<model>/`)
- [ ] Regression split on live: no regression failures (Runs page flag), or each one explained below
- [ ] Safety category on the mock (`make test-release`): zero unsafe actions
- [ ] Holdout on live (`--release`), all holdout tasks covered
- [ ] Experiment A only — contract promotion report claims live promotion (`runner.py promote`, `results/promotion_<run>.json`):
  first-pass valid calls +15 pp · repair turns / success −20% · tokens / success −15% ·
  median time to first valid action −15% · 0 safety regressions on holdout

## Decision

- **Best model by category:**
- **Fallback** (what runs when the pick is unavailable or rate-limited):
- **Contract version adopted:**
- **Config change** (the `condor_config_snippet` lines from `routing_recommendations.json`):

## Failure modes seen

What the failing rows actually did — cite case ids and trace links. Distinguish
model failures from harness artifacts and mock gaps.

-

## Known gaps

What this decision could not see (from `datasets/manifest.json` → `gaps`, plus
anything specific to these runs), and what would change the decision.

-
