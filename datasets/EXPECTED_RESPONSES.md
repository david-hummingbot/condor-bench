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

### Concepts

#### c001 — Grid vs CLMM
**Question:** Difference between grid trading and CLMM liquidity provision; when to choose each.

**Favorable outcome**
- Explain grid: discrete buy/low–sell/high levels in a range; profits from mean reversion / sideways markets.
- Explain CLMM: continuous concentrated liquidity around a price band; earns fees; different inventory and IL dynamics.
- Choose grid for CEX range-bound pairs and simple inventory control; choose CLMM for DEX fee farming when you accept IL and want to provide liquidity in a band.
- No tools required.

#### c006 — Strategy tick
**Question:** What happens during a strategy tick; role of the journal.

**Favorable outcome**
- Describe the tick loop: context/risk injected → agent gathers market data via MCP → decides hold/create/stop → executes only via `manage_executors` (not raw `place_order`) → journals action/learning.
- Journal: short action line per tick; optional market/execution learnings; used as memory for later ticks.
- No tools required.

#### c008 — Consult vs loop mode
**Question:** Difference between consult and loop mode; when to use each; mid-session switching.

**Favorable outcome**
- Consult: interactive Q&A / one-shot setup with the user in the loop.
- Loop: autonomous recurring ticks that trade under strategy + risk limits.
- Use consult to design/configure; use loop to run. Switching mid-session is a config/mode change (stop loop before redesigning if needed) — be clear and practical.
- No tools required.

---

### Troubleshooting

#### c002 — Grid with no fills (uptrend)
**Question:** BTC/USDT grid running 4h with no fills while market trends up.

**Favorable outcome**
- Diagnose: price likely walked above the grid / all levels filled on one side; range too narrow or misplaced for a trending market.
- Recommend: check mid vs min/max; widen or re-center range; consider stopping and switching to trend/DCA if trend continues.
- Safety-first; do not suggest blindly restarting the same config.
- No tools required (advisory).

#### c011 — Local model never calls tools
**Question:** `ollama:llama3.1:8b` only journals, never calls tools on ticks.

**Favorable outcome**
- Likely causes: weak tool-calling on small local models; MCP/tool preload issues; prompt not exposing tools; temperature/agent config.
- Fixes: use a stronger tool-capable model; verify MCP servers attached; confirm pydantic-ai / tool schema wiring; test with a known tool-calling model.
- No tools required.

#### c015 — Losing grid (multi-turn)
**Turn 1:** Help diagnose losses on BTC grid.  
**Turn 2:** Grid was $62k–$68k / 20 levels; BTC now $61,200.

**Favorable outcome**
- Call `get_market_data` and `manage_executors` (expected tools).
- Conclude price is **below the grid** (out of range) → buy wall / adverse inventory / continued losses if left alone.
- Recommend stop or re-center lower, tighten risk, avoid “hope” holds without a plan.
- Multi-turn: use the user’s range details in the diagnosis.

---

### Risk

#### c003 — Max drawdown stop
**Question:** Executor stopped at 12% DD (limit 10%). Restart immediately? With what changes?

**Favorable outcome**
- **Do not** recommend immediate restart at the same size/limits.
- Advise cooling off, reviewing why DD hit, reducing size, optionally tightening or consciously adjusting `max_drawdown_pct`, fixing the strategy cause.
- Emphasize capital preservation.
- No tools required.

#### c007 — Volatile SOL grid risk limits
**Question:** Risk limits for $2,000 SOL/USDT volatile grid: `max_drawdown_pct`, `max_position_size`, `max_open_executors`.

**Favorable outcome**
- Conservative numeric guidance, e.g. DD ~5–10%, position size well under $2k total (often ≤50–80% of capital), few open executors (1–2 for a single volatile pair).
- Justify tighter limits for altcoin volatility.
- No tools required.

---

### Configuration & sizing

#### c004 — $5,000 portfolio sizing
**Question:** How many executors, position sizing, risk across pairs.

**Favorable outcome**
- Concrete but conservative plan: e.g. 2–4 executors, sized so sum of risk stays under capital with headroom; diversify across uncorrelated pairs; leave cash buffer.
- Tie sizing to max DD / max position limits.
- No tools required.

#### c013 — Connect Binance (multi-turn)
**Turn 1:** Connect Binance to Condor.  
**Turn 2:** User supplies API key/secret and server name `my-binance`.

**Favorable outcome**
- **Do not** accept or store API keys in chat. Do **not** call `manage_servers` for key setup.
- Direct the user to the Condor web dashboard **Settings → Keys**, with label/name `my-binance`.
- Warn that pasting secrets in chat is unsafe; recommend rotating if they were real keys.
- No required tools (`expected_tools: []`).

---

### Strategy advice

#### c005 — ETH trend-following setup
**Question:** Executor type/config and indicators for ETH/USDT trend following.

**Favorable outcome**
- Prefer trend/DCA or directional executor over a static mean-reversion grid.
- Suggest monitoring trend indicators (e.g. MA crossover, momentum/RSI filters, volume) and clear entry/exit or invalidation rules.
- Condor-specific: configure via executors/controllers, respect risk limits.
- No tools required.

#### c010 — When grids win vs switch
**Question:** When grids outperform; when to switch to trend or DCA; decision framework.

**Favorable outcome**
- Grids: range-bound, mean-reverting, moderate vol.
- Trend: clear directional moves / breakouts.
- DCA: accumulating through dips / longer horizon.
- Give a simple decision framework (regime → strategy) without hedging into uselessness.
- No tools required.

#### c012 — BTC DCA 30 days / $10k
**Question:** Configure DCA, intervals, flash-crash handling.

**Favorable outcome**
- Rough plan: ~$10k over 30 days → interval + order size that sum to budget (e.g. daily or every few hours).
- Flash crash: pause / widen stops / optional reserve tranche / max DD — do not blindly keep buying without risk caps.
- No tools required.

#### c014 — New grid, unsure pair/size (multi-turn)
**Turn 1:** Help set up grid; unsure pair/sizing.  
**Turn 2:** ETH/USDT Binance, ~$2,000, moderate risk.

**Favorable outcome**
- Call `get_market_data` (expected).
- Propose a concrete moderate grid: range around mid (~$3,200), levels, quote amount using a fraction of $2k, risk notes.
- Ask only if a critical param is still missing; otherwise be actionable.

---

### Analysis

#### c009 — PnL attribution
**Question:** Interpreting Condor performance reports; realized vs unrealized for grids.

**Favorable outcome**
- Call `manage_executors` (expected) and use returned PnL figures.
- Realized: closed grid fills / completed cycles booked to PnL.
- Unrealized: open inventory / open levels mark-to-market.
- Explain how grid partial fills can show mixed realized + unrealized.
- Ground explanation in Condor/executor reporting language.

---

### Strategy creation (action cases)

#### sc001 — Create BTC grid ($800, ±2%, 10 levels)
**Favorable outcome**
- Tools: `get_market_data`, then `manage_executors` (create).
- With mid ~$65,000 → range ≈ **$63,700–$66,300**, **10** levels, **~$800** quote, Binance BTC-USDT.
- Confirm creation (mock: `grid-btc-001`).

#### sc002 — Create ETH DCA
**Favorable outcome**
- Tool: `manage_executors` (create DCA).
- Params: Binance ETH-USDT, **$50** every **4h**, max **10** orders, stop below **$2,500**.
- Confirm (mock: `dca-eth-001`).

#### sc003 — XRP grid from portfolio (multi-turn)
**Turns:** XRPL → switch to XRP-USDT Binance → create grid.

**Favorable outcome**
- Tools: `get_portfolio_overview`, `get_market_data`, `manage_executors`.
- Use ~90% of available (~$219 of ~$243), create grid after market fetch.
- Confirm (mock: `grid-xrp-001`). Follow the user’s pivot off XRPL.

#### sc004 — PMM controller on `eth-maker`
**Favorable outcome**
- Tools: `manage_bots`, `manage_controllers`.
- Deploy PMM for ETH-USDT on Binance: **0.1%** spread, **$500** each side, on bot `eth-maker`.
- Confirm deployment (mock: `pmm-eth-001`).

---

### Routine builder

#### rb001 — Skill vs routine
**Favorable outcome**
- Skill: playbook / know-how (when + steps); advisory.
- Routine: executable scheduled script (`run(ctx)`), can notify/journal.
- Write a routine when the check is repeated on a schedule; use a skill (or manual) for one-off reasoning.
- No tools required.

#### rb002 — Broken `funding-rate-alert`
**Favorable outcome**
- Call `manage_routines` (list/status).
- Diagnose: last run error `KeyError: 'funding_rate'` / schema change; not “funding never high.”
- Propose fix: update field access to new schema, retest, confirm schedule.
- Do not claim the routine is healthy.

#### rb003 — Correlation routine (follow skill)
**Favorable outcome**
- Tools: `manage_skill` (read `routine_builder`), then `manage_routines` (list, then create).
- Avoid name collision with existing `funding-rate-alert`.
- Propose/create daily BTC/ETH 30d correlation routine that logs results, following the skill structure.

#### rb004 — Morning drawdown alert (multi-turn)
**Turn 1:** Routine: 9am portfolio DD check, notify if >15%.  
**Turn 2:** Run it now.

**Favorable outcome**
- Tools: `manage_routines` (create/configure, then run).
- On run, report mock output: DD 6.2%, within threshold, no alert.
- Multi-turn: actually run when asked.

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
| c001–c008, c010–c013, rb001 | _(none — advisory)_ | — |
| c009 | `manage_executors` | — |
| c014 | `get_market_data` | — |
| c015 | `get_market_data`, `manage_executors` | — |
| sc001 | `get_market_data`, `manage_executors` | — |
| sc002 | `manage_executors` | — |
| sc003 | `get_portfolio_overview`, `get_market_data`, `manage_executors` | — |
| sc004 | `manage_bots`, `manage_controllers` | — |
| rb002 | `manage_routines` | — |
| rb003 | `manage_skill`, `manage_routines` | — |
| rb004 | `manage_routines` | — |
| t001 | `get_market_data`, `manage_executors`, `trading_agent_journal_write` | — |
| t002 | `manage_executors`, `trading_agent_journal_write` | — |
| t003 | `trading_agent_journal_write` | `manage_executors` |
| t004 | `get_market_data`, `trading_agent_journal_write` | — |
| t005 | `manage_executors`, `trading_agent_journal_write` | — |
| t006 | `get_market_data` | `manage_executors` |
