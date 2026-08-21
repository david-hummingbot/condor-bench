# Condor-bench case library

The published library is **80 live MCP cases**: 12 consult, 8 tick, 38 tool, 22
agent. That is the smallest set that still satisfies the routing floors in
[`tests/test_dataset_floors.py`](../tests/test_dataset_floors.py).

Do not add paraphrases to “cover” a tool that already has three hits. Do not
restore the everyday consult lookups that Layer 2 / Layer 3 already measure
(`c004`, `c005`, `c007`, `c008`, `c014`, `agent_condor_routine_003`). Favorable
outcomes live in [`datasets/EXPECTED_RESPONSES.md`](../datasets/EXPECTED_RESPONSES.md).

## Floors

| Axis | Bar | Smallest survivable sample |
|------|-----|----------------------------|
| Tool handled | `TOOL_PASS_RATE` 0.65 | **3** scored cases (2/3 still passes) |
| Domain owned | `DOMAIN_PASS_RATE` 0.80 | **5** scored cases (4/5 still passes) |

Hits accumulate across layers. A consult, a tool probe, and a specialist job that
all expect `manage_bots` are three hits on that tool — not three independent jobs.
Infra exclusions are dropped, not scored 0, so a tool sitting at exactly three
goes `thin` on one flaky live call.

`manage_notes` has no cases on purpose: it is a deprecated alias of
`manage_memory`. `manage_amm` is covered by `meteora_launch_lp` jobs, not a
dedicated Layer 2 triple.

## Core sweep

Cases tagged `core` (65) still meet both floors. Use them for harness/prompt
iteration:

```bash
uv run python runner.py test ollama:qwen2.5:14b --tags core
uv run python runner.py sweep --tags core
```

Publish routing from the **full** 80. A core run that silently dropped below
`MIN_TOOL_CASES` would reprint `thin` as if the tool were never benchmarked;
[`test_core_subset_clears_the_same_floors`](../tests/test_dataset_floors.py)
guards that.

## What not to author

- Layer A second samples for tools already at three hits
- Extra “read skill X and summarise” jobs on a specialist already at the domain floor
- Routing domains for strategy agents (`delta_neutral_funding_agent`,
  `xrpl_market_maker`, `smart_money_flow`, `adaptive_grid_trader`) — they inherit
  their base’s model assignment ([`datasets/agent_roles.json`](../datasets/agent_roles.json))
