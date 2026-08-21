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

Everyday consults are the coordinator lookups Layer 2 does not uniquely own
(`get_user_context`, `manage_servers`, `manage_trading_agent`). Portfolio, bots,
executors, history and funding-rate questions were dropped: the matching tool and
agent cases already supply those hits. See `docs/CASE_LIST.md`.

### Everyday usage

#### c002 — Condor user role
**Question:** What's my role in Condor, and do I have admin rights?

**Favorable outcome**
- Call `get_user_context` and report `user_role` and `is_admin` directly.
- Tools: `get_user_context`.

Asked about role rather than about the active server on purpose. `get_user_context`
returns `active_server`, but so does `manage_servers`, so the old wording ("what API
server am I connected to") was fully answerable without the tool this case exists to
measure — and a run answered it correctly via `manage_servers` for a composite of
0.665. `user_role` and `is_admin` are held by `get_user_context` alone.

#### c006 — Server online check
**Question:** Is my active Hummingbot API server online right now?

**Favorable outcome**
- Call `manage_servers` with `action="status"` (active server; do not hardcode a server name) and report online/offline plainly.
- Tools: `manage_servers`. Params: `action=status`.

#### c010 — Accessible servers
**Question:** List the API servers I have access to

**Favorable outcome**
- Call `manage_servers` (list) and enumerate server names/permissions, noting which is active.
- Tools: `manage_servers`.

#### c011 — Configured trading agents
**Question:** Which trading agents do I have set up, not counting yourself?

**Favorable outcome**
- Call `manage_trading_agent` with `action=list_agent_definitions` and summarise the
  trading agents, excluding the `condor` chat assistant.
- Tools: `manage_trading_agent`.

"Not counting yourself" is there because the tool answers wider than the old question
asked. `_list_agent_definitions` walks every `agents/*/AGENT.md`, and `condor` has one —
so the store returns 9 where the roster holds 8 trading agents plus the assistant. A run
listed the 8 and left itself out, which is the better answer, and the judge cut quality
from 1.0 to 0.55 for "silently omitting one agent". Now the sensible reading is the
expected one.

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
- The scenario's instructions ask for the `grid_executor` schema. They used to say
  `grid_strike`, which the API rejects with "Unknown executor type" — it is a
  *controller* (`handlers/bots/controllers/grid_strike/`), not an executor type, a
  conflation inherited from condor's own prompt example. The case was asking the model
  to make a call that could not succeed.
- Tools: `manage_executors` (multiple actions OK) → `trading_agent_journal_write`.
- End with a successful create when possible (retry: `sol-grid-002`).

### t006 — ETH dry run
**Scenario:** `execution_mode: dry_run`; observe only.

**Favorable outcome**
- Fetch market data; describe what you *would* do in conditional language; dry-run prefixes.
- Tools: `get_market_data` only.
- Must **not** call `manage_executors`.
- No live create/stop; no journal required for this experiment mode (and journal tools may be unavailable).

### t008 — SOL spread wider than the strategy minimum
**Scenario:** Flat book; spread 0.34% vs a 0.10% quoting minimum. Loop mode.

**Favorable outcome**
- Stand down and explain the spread. Create nothing.
- Tools: `get_market_data` → `trading_agent_journal_write`.
- The journal write is expected because this runs in `loop` mode, and the tick prompt
  mandates it: "Write ONE action entry per tick via `trading_agent_journal_write`". The
  case used to expect `get_market_data` alone, so an agent that obeyed the prompt scored
  tool F1 0.0 for doing the mandated thing.

### t009 — ETH learnings rule out a tempting tape
**Scenario:** RSI 79 looks strong; a prior learning forbids entries above RSI 75.
`execution_mode` is `dry_run`.

**Favorable outcome**
- Stand down and name the learning that stopped you. Create nothing.
- Tools: `get_market_data` only. Must **not** create or stop executors.
- `dry_run` is required: condor's three modes are `dry_run | run_once | loop`. A
  made-up mode falls through to `loop` and the live prompt then contradicts the
  "do not trade" instructions.

---

## Quick reference — Layer 1 expected tools

`risk` no longer gates whether a case runs — every case runs. It decides whether
teardown fires afterwards, and whether the case has to clear `DESTRUCTIVE_FLOOR`.
`slug` is the `agent_slug` the case is run under — blank means chat-scoped, which
is what a production consult does.

The chat-scoped `agent_condor_*` cases now live in `consult.jsonl`: they had
`agent_slug: null`, so they ran the same prompt against the same stores and already
pooled into `general_consult`. Keeping them in `agents.jsonl` implied a second
routing target that does not exist. `agents.jsonl` is specialists only — every case
in it names a slug.

This table mirrors the datasets and is maintained by hand — update it in the same
commit that changes case ground truth.

| ID | Expected tools | Pinned params | Must not call | Risk | Slug |
|----|----------------|---------------|---------------|------|------|
| c002 | `get_user_context` | — | — | read_only | — |
| c006 | `manage_servers` | `action=status` | — | read_only | — |
| c010 | `manage_servers` | `action=list` | — | read_only | — |
| c011 | `manage_trading_agent` | `action=list_agent_definitions` | — | read_only | — |
| agent_condor_005 | `manage_executors` | `action=create`, `trading_pair=RLUSD-XRP`, `connector_name=xrpl` | — | destructive | — |
| agent_condor_routine_001 | `manage_skill`, `manage_routines` | `action=read`, `action=create_routine`, `name=bench_btc_price` | — | mutating | — |
| agent_condor_routine_004 | `manage_routines` | `action=run`, `name=market_scanner` | — | mutating | — |
| agent_condor_builder_001 | — | — | `manage_trading_agent:create_strategy`, `manage_trading_agent:create_agent`, `manage_executors:create`, `manage_executors:stop` | read_only | — |
| agent_condor_builder_002 | `manage_skill`, `manage_trading_agent` | `action=read`, `action=create_strategy`, `name=bench_dca_sol` | — | destructive | — |
| agent_condor_delegate_001 | `delegate` | `agent=market_making_expert`, `action=start` | — | mutating | — |
| c_journal_roundtrip_001 | `trading_agent_journal_write`, `trading_agent_journal_read` | `agent_id=bench-journal-probe`, `entry_type=learning` | — | mutating | `bench_journal_probe` |
| c_journal_roundtrip_002 | `trading_agent_journal_write`, `trading_agent_journal_read` | `agent_id=bench-journal-probe`, `entry_type=decision`, `tick=12` | — | mutating | `bench_journal_probe` |
| t001 | `get_market_data`, `manage_executors`, `trading_agent_journal_write` | — | — | destructive | `bench_tick_normal` |
| t002 | `manage_executors`, `trading_agent_journal_write` | — | — | destructive | `bench_tick_profit` |
| t003 | `trading_agent_journal_write` | — | `manage_executors:create`, `manage_executors:stop` | mutating | `bench_tick_risk_blocked` |
| t004 | `get_market_data`, `trading_agent_journal_write` | — | — | mutating | `bench_tick_near_limit` |
| t005 | `manage_executors`, `trading_agent_journal_write` | — | — | destructive | `bench_tick_error_recovery` |
| t006 | `get_market_data` | — | `manage_executors:create`, `manage_executors:stop` | read_only | `bench_tick_dry_run` |
| t008 | `get_market_data`, `trading_agent_journal_write` | — | `manage_executors:create`, `manage_executors:stop` | read_only | `bench_tick_spread_wide` |
| t009 | `get_market_data` | — | `manage_executors:create`, `manage_executors:stop` | read_only | `bench_tick_learnings` |

`agent_condor_routine_001` reads `routine_cookbook` / `hummingbot_client.md` before
writing. Condor currently documents `get_prices` as a flat pair→price map; the API
returns `{"prices": {...}}`. Until that Condor fix lands, pre-flight rewrites the
known-bad snippet via `bench/skill_patches.py` so the case is not poisoned by the
doc bug (a live Binance price looked like a fetch failure, then a
`binance_paper_trade` guess).

`manage_notes` has no cases. condor's own docstring for it reads "DEPRECATED — use
manage_memory instead … New code should call manage_memory directly", it is a thin
alias (`set`→write, `get`→read), and no agent in the roster is granted it. Scoring a
model against it punished exactly the behaviour condor documents: a run called
`manage_memory` for "save a note", the judge scored the answer 0.9 and called the
save correct, and the case still landed at 0.47 because the pinned tool was never
touched. `manage_memory` carries the coverage instead.

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
