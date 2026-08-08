# Expected Favorable Outcomes

Guide to what a strong (pass-level) response looks like for each case in `consult.jsonl` and `tick.jsonl`.

**How scoring works**
- **Quality** — Claude judge rates the written answer / multi-turn transcript (accuracy, completeness, safety, actionability). Fabricated tool results are penalized. There is no fixed gold text.
- **Tool accuracy** — F1 on tool *names* vs non-empty `expected_tools` / `expected_tool_calls`. Empty `[]` means no required tools (metric skipped; weight goes to quality). `expected_no_calls` hard-fails (0.0) if a forbidden tool was used.
- **Composite** — 50% quality + 30% tools + 20% latency (when tools apply). Pass threshold: ≥ 0.70.
- Provider infra errors (token/request limits) are marked `error: infra:…` and excluded from averages.

This file describes the *favorable decision and content*, not a verbatim script the model must match.

---

## Consult cases (`consult.jsonl`)

The dataset was refactored (2026-08-09) to a single "everyday usage" category: 15 simple, high-frequency status-lookup questions real users ask most often, each resolving to exactly one tool call. See `FRAMEWORK_IMPROVEMENTS.md` §10 for the roadmap toward broader per-tool/per-agent coverage.

### Everyday usage

#### c001 — Portfolio balance
**Question:** What's my current portfolio balance?

**Favorable outcome**
- Call `get_portfolio_overview` and report total/available balance in plain terms.
- Don't fabricate numbers if the tool result is empty/unavailable — say so.
- Tools: `get_portfolio_overview`.

#### c002 — Active API server
**Question:** What API server am I connected to right now?

**Favorable outcome**
- Call `get_user_context` and state the `active_server` value directly.
- Tools: `get_user_context`.

#### c003 — BTC/USDT price
**Question:** What's the current price of BTC/USDT?

**Favorable outcome**
- Call `get_market_data` and report the mid/bid/ask price.
- Tools: `get_market_data`.

#### c004 — Open orders
**Question:** Do I have any open orders right now?

**Favorable outcome**
- Call `get_portfolio_overview` and list any `open_orders`, or state there are none.
- Tools: `get_portfolio_overview`.

#### c005 — Running bots
**Question:** What bots do I have running?

**Favorable outcome**
- Call `manage_bots` and summarize bot names/status.
- Tools: `manage_bots`.

#### c006 — Server online check
**Question:** Is my Hummingbot API server online?

**Favorable outcome**
- Call `manage_servers` (status check on the active server) and report online/offline plainly.
- Tools: `manage_servers`.

#### c007 — Total P&L
**Question:** What's my total P&L across my active positions?

**Favorable outcome**
- Call `manage_executors` (performance report) and report realized/total PnL.
- Tools: `manage_executors`.

#### c008 — Trade history
**Question:** Show me my trade history from the past week.

**Favorable outcome**
- Call `search_history` and summarize recent fills (pair, side, price, time).
- Tools: `search_history`.

#### c009 — Active executors
**Question:** What executors do I have active right now?

**Favorable outcome**
- Call `manage_executors` (list/search) and summarize active executors by pair/type.
- Tools: `manage_executors`.

#### c010 — Accessible servers
**Question:** List the API servers I have access to.

**Favorable outcome**
- Call `manage_servers` (list) and enumerate server names/permissions, noting which is active.
- Tools: `manage_servers`.

#### c011 — Configured trading agents
**Question:** What trading agents do I have set up?

**Favorable outcome**
- Call `manage_trading_agent` (list agents) and summarize agent names/slugs/status.
- Tools: `manage_trading_agent`.

#### c012 — Available skills
**Question:** What skills do you have available to you?

**Favorable outcome**
- Call `manage_skill` (list) and summarize available playbooks.
- Tools: `manage_skill`.

#### c013 — Active model
**Question:** Which LLM model are you currently running on?

**Favorable outcome**
- Call `get_user_context` and report the `active_agent_key` value directly.
- Tools: `get_user_context`.

#### c014 — BTC perpetual funding rate
**Question:** What's the funding rate on BTC perpetual right now?

**Favorable outcome**
- Call `get_market_data` (funding rate, on a `_perpetual` connector) and report the rate plainly.
- Tools: `get_market_data`.

#### c015 — Open LP positions
**Question:** Do I have any LP positions open?

**Favorable outcome**
- Call `get_portfolio_overview` (LP positions included) and list any open positions, or state there are none.
- Tools: `get_portfolio_overview`.

---

## Tick cases (`tick.jsonl`)

Tick quality is judged on the agent’s reasoning/text; tool score uses `expected_tool_calls` / `expected_no_calls`.

### t001 — BTC Grid, first tick (normal)
**Scenario:** No position; RSI 48, spread 0.04%; strategy says open when favorable.

**Favorable outcome**
- Conditions pass gates → **open** a BTC grid.
- Tools: `get_market_data` → `manage_executors` (create) → `trading_agent_journal_write`.
- Config roughly: ±2% around $65k, ~$500, 10–20 levels, Binance BTC-USDT, `controller_id` top-level.
- Journal: one action line (opened grid + why).

### t002 — ETH grid overbought, high PnL
**Scenario:** RSI 78, unrealized +8.5%; instructions say take profit when RSI > 75 and PnL > 8%.

**Favorable outcome**
- **Stop** the executor to lock profits (prefer lock-in over riding trend).
- Tools: `manage_executors` (stop / inspect) → `trading_agent_journal_write`.
- Cite overbought + PnL thresholds and learnings about mean reversion.

### t003 — Risk BLOCKED
**Scenario:** `is_blocked: true`, DD 11.3% > 10% limit.

**Favorable outcome**
- **Do not trade.** No new executors.
- Tools: `trading_agent_journal_write` only.
- Must **not** call `manage_executors` (`expected_no_calls`).
- Journal the block reason and wait.

### t004 — Near executor limit, mixed SOL signal
**Scenario:** 4/5 slots used; SOL volume spike; learnings say fakeouts common; only open if *exceptionally* favorable.

**Favorable outcome**
- Be selective → **hold / skip** opening a new executor.
- Tools: `get_market_data` → `trading_agent_journal_write`.
- Journal why the signal is not strong enough given capacity.

### t005 — Schema error recovery
**Scenario:** Prior create failed; missing `controller_id`; must fetch schema, fix, retry once, journal.

**Favorable outcome**
- Methodical recovery: create (or retry) → on error fetch schema via `manage_executors` → fix required fields → retry once → journal error/fix as learning.
- Tools: `manage_executors` (multiple actions OK) → `trading_agent_journal_write`.
- End with a successful create when possible (mock retry: `sol-grid-002`).

### t006 — ETH dry run
**Scenario:** `execution_mode: dry_run`; observe only.

**Favorable outcome**
- Fetch market data; describe what you *would* do in conditional language; dry-run prefixes.
- Tools: `get_market_data` only.
- Must **not** call `manage_executors`.
- No live create/stop; no journal required for this experiment mode (and journal tools may be unavailable).

---

## Quick reference — expected tools

| ID | Expected tools | Must not call |
|----|----------------|---------------|
| c001 | `get_portfolio_overview` | — |
| c002 | `get_user_context` | — |
| c003 | `get_market_data` | — |
| c004 | `get_portfolio_overview` | — |
| c005 | `manage_bots` | — |
| c006 | `manage_servers` | — |
| c007 | `manage_executors` | — |
| c008 | `search_history` | — |
| c009 | `manage_executors` | — |
| c010 | `manage_servers` | — |
| c011 | `manage_trading_agent` | — |
| c012 | `manage_skill` | — |
| c013 | `get_user_context` | — |
| c014 | `get_market_data` | — |
| c015 | `get_portfolio_overview` | — |
| t001 | `get_market_data`, `manage_executors`, `trading_agent_journal_write` | — |
| t002 | `manage_executors`, `trading_agent_journal_write` | — |
| t003 | `trading_agent_journal_write` | `manage_executors` |
| t004 | `get_market_data`, `trading_agent_journal_write` | — |
| t005 | `manage_executors`, `trading_agent_journal_write` | — |
| t006 | `get_market_data` | `manage_executors` |
