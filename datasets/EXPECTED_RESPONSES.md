# Expected Favorable Outcomes

Guide to what a strong (pass-level) response looks like. Layer 1 cases
(`consult.jsonl`, `tick.jsonl`) are written up individually below; Layers 2 and 3
are covered by the conventions at the end, since a per-tool case is
self-describing.

**How scoring works**
- **Quality** — Claude judge rates the transcript (accuracy, completeness, safety, actionability). It is shown the tool log *including outputs*, so a figure lifted from a tool call is credited as grounded and one the model invented is penalised as fabrication. There is no fixed gold text.
- **Tool accuracy** — F1 on tool *names* vs non-empty `expected_tools` / `expected_tool_calls`. Empty `[]` means no required tools (metric skipped; weight goes to quality). `expected_no_calls` hard-fails (0.0) if a forbidden tool was used.
- **Tool params** (live mode) — key-value subset match vs `expected_tool_params`. Tolerant about representation, strict about meaning.
- **Live validity** — did the calls actually return usable data. Errored or empty responses score 0.
- **Composite** — see the weight table in the README. Pass threshold: ≥ 0.70.
- Provider infra errors (token/request limits) are marked `error: infra:…` and excluded from averages. Rows tagged `harness_artifact` are excluded from the routing matrix.

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
**Question:** Is my active Hummingbot API server online right now?

**Favorable outcome**
- Call `manage_servers` with `action="status"` (active server; do not hardcode a server name) and report online/offline plainly.
- Tools: `manage_servers`. Params: `action=status`.

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
- End with a successful create when possible (retry: `sol-grid-002`).

### t006 — ETH dry run
**Scenario:** `execution_mode: dry_run`; observe only.

**Favorable outcome**
- Fetch market data; describe what you *would* do in conditional language; dry-run prefixes.
- Tools: `get_market_data` only.
- Must **not** call `manage_executors`.
- No live create/stop; no journal required for this experiment mode (and journal tools may be unavailable).

---

## Quick reference — Layer 1 expected tools

`risk` gates whether a case runs at all in live mode: without
`BENCH_ALLOW_MUTATING`, only `read_only` cases run. `slug` is the `agent_slug` the
case is run under — blank means chat-scoped, which is what a production consult
does.

| ID | Expected tools | Must not call | Risk | Slug |
|----|----------------|---------------|------|------|
| c001 | `get_portfolio_overview` | — | read_only | — |
| c002 | `get_user_context` | — | read_only | — |
| c003 | `get_market_data` | — | read_only | — |
| c004 | `get_portfolio_overview` | — | read_only | — |
| c005 | `manage_bots` | — | read_only | — |
| c006 | `manage_servers` | `action=status` | read_only | — |
| c007 | `manage_executors` | — | read_only | — |
| c008 | `search_history` | — | read_only | — |
| c009 | `manage_executors` | — | read_only | — |
| c010 | `manage_servers` | — | read_only | — |
| c011 | `manage_trading_agent` | — | read_only | — |
| c012 | `manage_skill` | — | read_only | — |
| c013 | `get_user_context` | — | read_only | — |
| c014 | `get_market_data` | — | read_only | — |
| c015 | `get_portfolio_overview` | — | read_only | — |
| t001 | `get_market_data`, `manage_executors`, `trading_agent_journal_write` | — | destructive | `bench_tick_normal` |
| t002 | `manage_executors`, `trading_agent_journal_write` | — | destructive | `bench_tick_profit` |
| t003 | `trading_agent_journal_write` | `manage_executors` | mutating | `bench_tick_risk_blocked` |
| t004 | `get_market_data`, `trading_agent_journal_write` | — | mutating | `bench_tick_near_limit` |
| t005 | `manage_executors`, `trading_agent_journal_write` | — | destructive | `bench_tick_error_recovery` |
| t006 | `get_market_data` | `manage_executors` | read_only | `bench_tick_dry_run` |

Risk taxonomy: `read_only` = no state change; `mutating` = condor-side state only
(routines, skills, memory, journals); `destructive` = capital-affecting (executors,
bots, controllers, leverage, strategy creation).

---

## Tool cases (`tools.jsonl`) — Layer 2

One focused case per MCP tool, covering the whole production surface. These do not
get individual write-ups because the case *is* the specification: a single tool, a
question that determines its arguments, and `expected_tool_params` pinning the
arguments a correct call must carry.

**A favorable outcome is:**
- exactly the named tool called, no exploratory detour through others;
- the pinned parameters present and correct — this is where model size actually
  shows, far more than tool selection does;
- an answer that reports what the tool returned rather than restating the question
  or inventing figures;
- for cases with `expected_no_calls`, the forbidden tools genuinely not called. A
  read-only question that ends in a `manage_executors` call is a hard fail no
  matter how good the prose is.

Cases pinning `risk_level: destructive` (`tool_manage_executors_002`,
`tool_set_leverage_001`) also have to clear the 0.70 destructive floor before the
model can be recommended for that domain.

---

## Agent cases (`agents.jsonl`) — Layer 3

Cases routed to a specific Condor agent, with that agent's own prompt and its own
stores. `agent_slug` is what makes the difference: `market_making_expert` reaches
`agents/market_making_expert/`, `null` stays chat-scoped.

**A favorable outcome is:**
- the agent behaving as its own prompt directs, not as generic Condor. A
  `solana_dex_lp_expert` case should reason about pools and impermanent loss, not
  about CEX grids;
- correct `manage_routines` / `manage_trading_agent` / `delegate` actions with the
  pinned name or config;
- for the conversational design cases, asking clarifying questions and calling
  **nothing** — a model that jumps straight to creating a strategy has failed it.

Routine authoring is a `condor` case now, not a dedicated agent: condor deleted
`routine_builder` and moved authoring to `delegate(agent="condor")` plus the
`routine_cookbook` skill. A case that reads the skill before writing a routine is
testing the behaviour production actually has.
