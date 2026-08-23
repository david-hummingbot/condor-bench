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

## Declaring markets instead of hardcoding one

A case that names a venue is making a claim about the box the run lands on, and
when the claim is wrong the *model* takes the score: it calls
`binance_perpetual`, gets `missing 2 required positional arguments:
'binance_api_key'` back, and Live Validity reads that as a failed tool call.

So a case that depends on a tradeable market declares what it needs and lets the
target pick:

```json
{"id": "tool_set_leverage_001",
 "markets": {"perp": {"kind": "perpetual", "needs": "credentials",
                      "prefer": ["binance_perpetual", "binance_perpetual_testnet"],
                      "pair": "BTC-USDT"}},
 "question": "Set {perp.label} to 3x leverage in one-way position mode for {perp.pair}.",
 "expected_tool_params": {"set_account_position_mode_and_leverage":
   {"leverage": 3, "trading_pair": "{perp.pair}",
    "connector_name": "{perp.connector}"}}}
```

| Key | Meaning |
|-----|---------|
| `kind` | `perpetual` / `spot`. Omit when either will do |
| `needs` | `credentials` (acts as the account) or `support` (public data only) |
| `namespace` | `cex` (default) or `gateway` for DEX/AMM connectors |
| `prefer` | tried in order before anything else; put the venue the case was written for first |
| `pair` / `pairs` | first one the bound connector actually lists wins |
| `allow_kind_change` | permit spot → perpetual when nothing same-kind fits; the swap is recorded |

Placeholders `{name.connector}`, `{name.label}`, `{name.pair}`,
`{name.account}` and `{name.kind}` are substituted through *every* field, so the
question the model reads and the ground truth it is scored against always name
the same market. A placeholder no requirement declares refuses the run rather
than reaching the model verbatim.

Three rules worth internalising:

- **Put the original venue first in `prefer`.** Resolution is deterministic —
  `prefer` order, then alphabetical — so a box that has the real connector keeps
  using it, and only a box that does not falls back.
- **Declare the requirement even when the prose names no venue.**
  `tool_set_leverage_003` asks "Put BTC-USDT perpetuals back to 2x leverage" and
  names nothing; the tool still requires `connector_name`, so it declares a
  perpetual market. Without that, a box with only spot keys runs it and scores
  the model on an impossible call.
- **Do not pin `connector_name` when the question deliberately omits the venue.**
  `_003` binds the pair but leaves the connector unpinned, because any
  credentialed perp is a defensible answer to a question that never said which.

Unbound requirements refuse the run (`bench/market_resolver.py`), and
`make market-check` reports the whole library against the current target. See
[docs/STAGING.md](STAGING.md#connector-availability-make-market-check).

## What not to author

- Layer A second samples for tools already at three hits
- Extra “read skill X and summarise” jobs on a specialist already at the domain floor
- Routing domains for strategy agents (`delta_neutral_funding_agent`,
  `xrpl_market_maker`, `smart_money_flow`, `adaptive_grid_trader`) — they inherit
  their base’s model assignment ([`datasets/agent_roles.json`](../datasets/agent_roles.json))
