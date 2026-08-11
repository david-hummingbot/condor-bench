# Condor-bench case list — grant-aligned recommendations

Sketch of the exact cases needed so a model can be recommended for a Condor
agent role. **Grants come from each agent’s `AGENT.md` `tools:` list** — do not
invent specialist ownership for tools nobody declares.

This is a plan, not a dataset dump. **Parts of it are superseded** — the tool bar is 0.65 over 3 cases (not 0.80 over 2), `BENCH_ALLOW_MUTATING` no longer exists, and three agents here are now strategies rather than routing domains. Status markers:

| marker | meaning |
|--------|---------|
| **keep** | already in dataset; reuse as-is |
| **fix** | exists but wrong grant / wrong layer — rewrite |
| **add** | new case to write |

---

## Recommendation gates

| Role | Gate | Evidence |
|------|------|----------|
| **Tool competence** | each of 24 tools has ≥`MIN_TOOL_CASES` (3) scored cases at ≥`TOOL_PASS_RATE` (0.65) | Layer A (`type: tool`) |
| **Specialist** (MM, SOL, DN, XRPL, MET) | tool competence on **that grant** *and* Layer B job cases with `agent_slug` set pass (≥80%, ≥3 scored, `DESTRUCTIVE_FLOOR`) | Layer A ∩ grant + Layer B |
| **General consult / condor** | tool competence on orphan + coordinator tools *and* Layer C job cases | Layer A orphans + Layer C |
| **Directional / smart_money** | Layer B job cases (they inherit all 24 — selection alone is a weak discriminator) | Layer B only for role; Layer A still required for full-surface claim |
| **Tick defaults** | ≥5 read-only ticks scored + mutating/destructive floor cases | Layer D |

Hard AND across every grant tool is too brittle for routing. Prefer:

- each granted tool: ≥2 scored tool-competence passes, **or** exercised inside a passing job case, and
- domain pass-rate ≥80% on the Layer B set for that `agent_slug`.

Promote to “recommended for `agents/<slug>/agent_key`” only when **both** hold.

---

## Layer A — Tool competence (≥2 probes per tool)

Naked `type: tool` cases on the full surface. Measure “can call this MCP tool.”
They do **not** load specialist `AGENT.md` / skills — do not use them alone to
recommend a specialist.

| Tool | Existing | Need | Cases to keep / add |
|------|----------|------|---------------------|
| `get_market_data` | 2 | 0 | **keep** `tool_get_market_data_001`, `_002` |
| `manage_bots` | 2 | 0 | **keep** `tool_manage_bots_001`, `_002` |
| `manage_executors` | 2 | 0 | **keep** `tool_manage_executors_001` (list), `_002` (create, destructive) |
| `manage_routines` | 2 | 0 | **keep** `tool_manage_routines_001`, `_002` |
| `get_portfolio_overview` | 1 | +1 | **keep** `_001` · **add** `tool_get_portfolio_overview_002` — “Show open orders only; omit balances.” |
| `manage_controllers` | 1 | +1 | **keep** `_001` · **add** `tool_manage_controllers_002` — “Show config for controller `generic/pmm_mister`” (read) |
| `search_history` | 1 | +1 | **keep** `_001` · **add** `tool_search_history_002` — “Last 10 trades for ETH-USDT on Binance.” |
| `explore_dex_pools` | 1 | +1 | **keep** `_001` · **add** `tool_explore_dex_pools_002` — “Pool info for a given Solana pool address” (`action=info` / detail) |
| `explore_geckoterminal` | 1 | +1 | **keep** `_001` · **add** `tool_explore_geckoterminal_002` — “Trending pools on Solana network.” |
| `manage_skill` | 1 | +1 | **keep** `_001` · **add** `tool_manage_skill_002` — “List available skills.” |
| `manage_memory` | 1 | +1 | **keep** `_001` (write) · **add** `tool_manage_memory_002` — “List memories” / read `risk_ceiling` |
| `manage_notes` | 1 | +1 | **keep** `_001` · **add** `tool_manage_notes_002` — “Write a scratch note: bench probe.” |
| `manage_servers` | 1 | +1 | **keep** `_001` · **add** `tool_manage_servers_002` — “Status of the active server.” |
| `configure_server` | 1 | +1 | **keep** `_001` · **add** `tool_configure_server_002` — “Show current server connection without changing it.” |
| `get_user_context` | 1 | +1 | **keep** `_001` · **add** `tool_get_user_context_002` — “What preferences / active server do you know about me?” |
| `get_available_models` | 1 | +1 | **keep** `_001` · **add** `tool_get_available_models_002` — “Which models can I assign to a trading agent?” (worded differently) |
| `manage_trading_agent` | 1 | +1 | **keep** `_001` · **add** `tool_manage_trading_agent_002` — “List strategies” (`action=list_strategies`) |
| `consult` | 1 | +1 | **keep** `_001` · **add** `tool_consult_001`→keep · **add** `tool_consult_002` — “Ask `solana_dex_lp_expert` which SOL pool looks best to LP; do not open.” |
| `delegate` | 1 | +1 | **keep** `_001` · **add** `tool_delegate_002` — “Start a delegate to `delta_neutral_funding_agent` to review funding carry, then show task status.” |
| `send_notification` | 1 | +1 | **keep** `_001` · **add** `tool_send_notification_002` — “Notify me: funding check complete.” |
| `set_account_position_mode_and_leverage` | 1 | +1 | **keep** `_001` (destructive) · **add** `tool_set_leverage_002` — RO if API allows “get current leverage”; else second mutating pair with teardown |
| `trading_agent_journal_read` | 1 | +1 | **keep** `_001` · **add** `tool_journal_read_002` — “Read learnings for agent `bench-journal-probe`.” |
| `trading_agent_journal_write` | 1 | +1 | **keep** `_001` · **add** `tool_journal_write_002` — “Write entry_type=learning about spread too tight.” |
| `manage_amm` | **0** | +2 | **add** `tool_manage_amm_001` — “Load the manage_amm guide” (no action / empty) · **add** `tool_manage_amm_002` — “List positions owned on Meteora Solana” (`positions_owned`, prefer RO) |

**Layer A delta:** ~22 new tool probes (mostly second samples + 2× `manage_amm`).

Orphan / coordinator tools (no specialist grant) — competence here feeds **general_consult**, never a specialist:

`configure_server`, `manage_servers`, `get_user_context`, `get_available_models`, `manage_notes`, `consult`, `delegate`, `manage_trading_agent`, `set_account_position_mode_and_leverage`, `trading_agent_journal_*`.

---

## Layer B — Specialist job cases (`agent_slug` set)

Job tests under the **restricted grant**. Every case: domain-flavored prompt, `expected_tools` ⊆ grant, `expected_no_calls` on creates for advisory rows, `live_expected` where possible.

Target: **≥8 cases / specialist**, ≥5 read-only, so `min_cases=3` still holds after exclusions. Prefer RO-heavy for default sweeps.

### `market_making_expert` (grant: 8)

`get_market_data`, `get_portfolio_overview`, `manage_executors`, `manage_controllers`, `manage_bots`, `search_history`, `manage_memory`, `manage_skill`

| ID | Status | Risk | Tools | Prompt sketch |
|----|--------|------|-------|---------------|
| `agent_market_making_expert_001` | **keep** | RO | `get_market_data` | BTC-USDT regime / 0.2% spread too tight? Do not create. |
| `agent_mm_002` | **fix** ← was `_002` | destructive | `manage_skill`, `manage_controllers` | Read `pmm_config_playbook`, propose / upsert a `generic/pmm_mister` controller config for ETH-USDT $500 max 5% DD. **Do not** use `manage_trading_agent` (not in grant). |
| `agent_mm_003` | **fix** ← was `_003` | RO | `manage_bots` | List deployed bots and say which look like PMM makers. |
| `agent_mm_004` | **add** | RO | `get_portfolio_overview` | Inventory skew on BTC-USDT — too heavy base to keep quoting both sides? Do not change bots. |
| `agent_mm_005` | **add** | RO | `manage_controllers` | List controller configs; which are PMM-related? |
| `agent_mm_006` | **add** | RO | `manage_executors` | List executors across bots; any stuck / should pause quoting? Do not stop. |
| `agent_mm_007` | **add** | RO | `search_history` | Recent fills on the maker — adverse selection or fine? |
| `agent_mm_008` | **add** | RO | `manage_skill` | Read `mm_bot_report` (or `capital_allocation`) and summarise how you’d size a new PMM. |
| `agent_mm_009` | **add** | mutating | `manage_memory` | Remember: prefer ≥0.08% TP on binance_perpetual after fee check. |
| `agent_mm_010` | **add** | destructive | `manage_skill`, `manage_bots` | Read `pmm_mister_deploy`, then deploy/start path for a bench PMM (teardown required). Optional shortlist-only. |

**Note:** Existing `_002` / `_003` expect `manage_trading_agent`, which MM does **not** declare. Treat as broken grant signal until fixed.

### `solana_dex_lp_expert` (grant: 8)

`explore_geckoterminal`, `explore_dex_pools`, `manage_executors`, `get_portfolio_overview`, `get_market_data`, `search_history`, `manage_memory`, `manage_skill`

| ID | Status | Risk | Tools | Prompt sketch |
|----|--------|------|-------|---------------|
| `agent_solana_dex_lp_expert_001` | **keep** | RO | `explore_dex_pools` | Deepest SOL-USDC pools; which to LP? Do not open. |
| `agent_solana_dex_lp_expert_002` | **keep** | RO | `manage_skill` | Read `lp_range_config`; propose range given vol / IL. |
| `agent_sol_003` | **add** | RO | `explore_geckoterminal` | Trending Solana memecoin pools by volume; top 3 fee/TVL candidates. Do not open. |
| `agent_sol_004` | **add** | RO | `manage_skill` | Read `pool_ranking`; explain ranking rubric, apply to current scan. |
| `agent_sol_005` | **add** | RO | `get_portfolio_overview` | Any open LP executor slots? Hold vs exit at a high level. Do not close. |
| `agent_sol_006` | **add** | RO | `get_market_data` | SOL spot/perp context for sizing a new LP slot. |
| `agent_sol_007` | **add** | RO | `search_history` | Recent LP executor history — churning or stable? |
| `agent_sol_008` | **add** | RO | `manage_skill` | Read `slot_exit`; when would you TP/SL a slot? |
| `agent_sol_009` | **add** | mutating | `manage_memory` | Save preferred `lp_provider` / base_pct policy. |
| `agent_sol_010` | **add** | destructive | `manage_executors` | Open a tiny LP executor on a named pool (teardown). Shortlist-only. |

### `delta_neutral_funding_agent` (grant: 9)

> **Superseded — not a routing domain.** `delta_neutral_funding_agent` is a user-created *strategy*, a specialisation of a base specialist rather than a role bench sizes a model for. It is in `bench.dataset.STRATEGY_AGENTS`, excluded from `CONDOR_CONFIG_KEYS`, and its evidence comes from Layer A tool competence. The Layer B table below is kept for reference only — do not author these cases.

`get_market_data`, `get_portfolio_overview`, `manage_executors`, `manage_controllers`, `manage_bots`, `manage_routines`, `search_history`, `manage_memory`, `manage_skill`

| ID | Status | Risk | Tools | Prompt sketch |
|----|--------|------|-------|---------------|
| `agent_delta_neutral_funding_001` | **keep** | RO | `get_market_data` | BTC perp funding — worth opening DN capture? Do not open. |
| `agent_delta_neutral_funding_002` | **keep** | RO | `get_portfolio_overview` | Is the book actually delta neutral? |
| `agent_dn_003` | **add** | RO | `get_market_data` | Compare funding on a HIP-3 / configured pair vs hedge leg. |
| `agent_dn_004` | **add** | RO | `manage_bots` | List bots; which look like the DN funding MM? |
| `agent_dn_005` | **add** | RO | `manage_controllers` | List configs relevant to pair MM / funding. |
| `agent_dn_006` | **add** | RO | `manage_executors` | Open executors — net exposure look hedged? Do not change. |
| `agent_dn_007` | **add** | RO | `search_history` | Funding / trade history — is carry being harvested? |
| `agent_dn_008` | **add** | RO | `manage_routines` | List routines; any funding monitors already defined? |
| `agent_dn_009` | **add** | mutating | `manage_routines` | Create `bench_funding_probe` one-shot that returns current funding. |
| `agent_dn_010` | **add** | mutating | `manage_memory` | Remember hedge-beta refresh rule for the active pair. |

(No skills dir upstream — skip `manage_skill` job cases or use list/read only if skills appear later.)

### `xrpl_market_maker` (grant: 9)

> **Superseded — not a routing domain.** `xrpl_market_maker` is a user-created *strategy*, a specialisation of a base specialist rather than a role bench sizes a model for. It is in `bench.dataset.STRATEGY_AGENTS`, excluded from `CONDOR_CONFIG_KEYS`, and its evidence comes from Layer A tool competence. The Layer B table below is kept for reference only — do not author these cases.

`get_market_data`, `get_portfolio_overview`, `explore_geckoterminal`, `manage_executors`, `manage_controllers`, `manage_bots`, `manage_routines`, `manage_skill`, `send_notification`

| ID | Status | Risk | Tools | Prompt sketch |
|----|--------|------|-------|---------------|
| `agent_xrpl_001` | **add** | RO | `get_market_data` | XRPL / reference price for a pair — is spread viable vs AMM? Do not place offers. |
| `agent_xrpl_002` | **add** | RO | `get_portfolio_overview` | Balances / trustline constraints for sizing offers. |
| `agent_xrpl_003` | **add** | RO | `explore_geckoterminal` | External reference price / depth for the token. |
| `agent_xrpl_004` | **add** | RO | `manage_bots` | List bots; any XRPL makers deployed? |
| `agent_xrpl_005` | **add** | RO | `manage_controllers` | List configs usable for XRPL CLOB MM. |
| `agent_xrpl_006` | **add** | RO | `manage_executors` | Active executors — inventory OK to keep quoting? |
| `agent_xrpl_007` | **add** | RO | `manage_routines` | List routines related to XRPL price / spread checks. |
| `agent_xrpl_008` | **add** | RO | `manage_skill` | Read `xrpl_mm_deploy`; summarise deploy checklist. |
| `agent_xrpl_009` | **add** | mutating | `send_notification` | Notify: XRPL spread check complete. |
| `agent_xrpl_010` | **add** | destructive | `manage_skill`, `manage_bots` | Deploy path per skill (shortlist / `BENCH_ALLOW_MUTATING`). |

### `meteora_launch_lp` (grant: 7) — exclusive `manage_amm`

`manage_amm`, `explore_dex_pools`, `get_portfolio_overview`, `get_market_data`, `send_notification`, `manage_memory`, `manage_skill`

| ID | Status | Risk | Tools | Prompt sketch |
|----|--------|------|-------|---------------|
| `agent_met_001` | **add** | RO | `manage_amm` | Load manage_amm guide; summarise DAMM v2 add/remove flow. |
| `agent_met_002` | **add** | RO | `manage_amm` | `positions_owned` on Meteora — what is open? Do not remove. |
| `agent_met_003` | **add** | RO | `explore_dex_pools` | Find established Meteora DAMM v2 pools with fee yield; rank. Do not add. |
| `agent_met_004` | **add** | RO | `get_market_data` | SOL/memecoin context for sizing an early LP. |
| `agent_met_005` | **add** | RO | `get_portfolio_overview` | Balances available for a new two-sided add. |
| `agent_met_006` | **add** | RO | `manage_skill` | Read `launch_safety_check`; list graduation gates. |
| `agent_met_007` | **add** | mutating | `manage_memory` | Save: skip launches failing safety checklist X. |
| `agent_met_008` | **add** | mutating | `send_notification` | Notify: launch candidate rejected by safety check. |
| `agent_met_009` | **add** | destructive | `manage_amm` | Tiny `add_liquidity` on a staging pool (teardown). **No `_UNDO` today — staging only / shortlist.** |

### `directional_trader` (inherit all 24 — job cases are the role gate)

Skills: `backtesting`, `controller_development`, `deploy_and_monitor`, `research`

| ID | Status | Risk | Tools | Prompt sketch |
|----|--------|------|-------|---------------|
| `agent_directional_trader_001` | **keep** | RO | `get_market_data` | ETH 1h candles — long supported? Do not deploy. |
| `agent_directional_trader_002` | **keep** | RO | `manage_skill` | Read `backtesting`; how validate before deploy? |
| `agent_dir_003` | **add** | RO | `manage_skill` | Read `research`; outline signal pipeline for EMA trend on BTC. |
| `agent_dir_004` | **add** | RO | `manage_skill` | Read `controller_development`; what must `update_processed_data` guarantee? |
| `agent_dir_005` | **add** | RO | `manage_controllers` | List controllers; any directional already uploaded? |
| `agent_dir_006` | **add** | RO | `search_history` | Live vs recent fills — does live match backtest expectation qualitatively? |
| `agent_dir_007` | **add** | RO | `manage_routines` | List research routines useful for signal exploration. |
| `agent_dir_008` | **add** | mutating | `manage_routines` | Create a one-shot research routine that returns EMA signal summary for BTC-USDT. |
| `agent_dir_009` | **add** | RO | `get_portfolio_overview` | Open directional exposure — flat enough to consider a new long? |
| `agent_dir_010` | **add** | destructive | `manage_skill`, `manage_controllers` | Read `deploy_and_monitor`; upload/deploy path (shortlist). |

### `smart_money_flow` (inherit all 24)

> **Superseded — not a routing domain.** `smart_money_flow` is a user-created *strategy*, a specialisation of a base specialist rather than a role bench sizes a model for. It is in `bench.dataset.STRATEGY_AGENTS`, excluded from `CONDOR_CONFIG_KEYS`, and its evidence comes from Layer A tool competence. The Layer B table below is kept for reference only — do not author these cases.

Skill: `smart_money_playbook`

| ID | Status | Risk | Tools | Prompt sketch |
|----|--------|------|-------|---------------|
| `agent_smf_001` | **add** | RO | `manage_skill` | Read `smart_money_playbook`; summarise flow composite. |
| `agent_smf_002` | **add** | RO | `explore_geckoterminal` | Solana DeFi pulse — top pools volume/TVL momentum. Do not trade. |
| `agent_smf_003` | **add** | RO | `get_market_data` | BTC dominance / major perp context for risk regime. |
| `agent_smf_004` | **add** | RO | `get_portfolio_overview` | Current Derive / perp book — already in a directional bet? |
| `agent_smf_005` | **add** | RO | `search_history` | Recent smart-money strategy fills vs HOLD periods. |
| `agent_smf_006` | **add** | RO | `manage_executors` | List executors; any live SMF positions? Do not change. |
| `agent_smf_007` | **add** | mutating | `manage_memory` | Remember: HOLD when flow composite conflicted last session. |
| `agent_smf_008` | **add** | RO | `manage_skill`, `get_market_data` | Playbook + live data → LONG/SHORT/HOLD on BTC; do not open. |
| `agent_smf_009` | **add** | destructive | `manage_executors` | Open tiny Derive directional (staging / shortlist). |
| `agent_smf_010` | **add** | mutating | `send_notification` | Notify HOLD decision with one-line reason. |

---

## Layer C — `general_consult` / condor (full surface)

Coordinator + orphan tools + thin shared reads. After merging null-slug agent rows into consult, **trim duplicates** so GC stays ~10–15 scored RO, not 28+.

| ID | Status | Risk | Tools | Prompt sketch |
|----|--------|------|-------|---------------|
| `c001`–`c015` | **keep selective** | RO | various | Keep distinct everyday prompts; drop near-dupes of `agent_condor_001`–`004` after merge. |
| `agent_condor_001`–`004` | **merge → consult** then trim | RO | market/portfolio/bots/history | Hygiene only — same domain as GC. |
| `agent_condor_006` | **keep** as GC | mutating | `manage_notes` | Notes write — orphan tool. |
| `agent_condor_delegate_001` | **keep** | mutating | `delegate` | Hand off to MM expert. |
| `tool_consult_*` | Layer A | RO | `consult` | Also counts toward GC coordinator competence. |
| `gc_models_001` | **add** (or rely on tool layer) | RO | `get_available_models` | Which LLMs can I assign? |
| `gc_context_001` | **keep** via `c002`/`c013` | RO | `get_user_context` | Setup / model context. |
| `gc_servers_001` | **keep** via `c006`/`c010` | RO | `manage_servers` | Server list/status. |
| `gc_configure_001` | **add** job wrap | RO | `configure_server` | What server am I pointed at? |
| `gc_agents_001` | **keep** `c011` | RO | `manage_trading_agent` | List trading agents. |
| `agent_condor_routine_001`–`004` | **keep** (builds) | mix | skill + routines | Shortlist for overnight; not required for GC recommend. |
| `agent_condor_builder_001` | **keep** | RO | none | Conversation-only design gate. |
| `agent_condor_builder_002` | **keep** | destructive | skill + trading_agent | Shortlist build. |

**Recommend as condor** when: Layer A orphans/coordinator mostly green **and** Layer C everyday + consult/delegate pass-rate ≥80%.

---

## Layer D — `tick_execution`

Today: 6 ticks, only `t006` is RO. Need ≥5 RO for default-ceiling recommendations.

| ID | Status | Risk | Expected | Prompt / scenario sketch |
|----|--------|------|----------|--------------------------|
| `t006` | **keep** | RO | `get_market_data`; no `manage_executors` | Dry-run observe. |
| `t003` | **keep** | mutating | journal write; no create | Risk blocked — journal only. (Counts when mutating allowed; still useful.) |
| `t007` | **add** | RO | `get_market_data`, `trading_agent_journal_write` | Observe-only tick: favorable tape but instructions say journal HOLD, never create. |
| `t008` | **add** | RO | `trading_agent_journal_write` | No-op tick: already flat, no signal — journal HOLD. |
| `t009` | **add** | RO | `trading_agent_journal_read` optional / write | Read prior learnings from prompt context; journal that you respected them. Prefer write-only if read is injected. |
| `t010` | **add** | RO | `get_market_data` | Spread too wide vs strategy min — do not open; describe why. |
| `t011` | **add** | RO | journal write; `expected_no_calls: [manage_executors]` | Explicit “do not trade this tick.” |
| `t001`,`t002`,`t005` | **keep** | destructive | create / stop / recovery | Floor gates under `BENCH_ALLOW_MUTATING` + `DESTRUCTIVE_FLOOR`. |
| `t004` | **keep** | mutating | market_data + journal | Near capacity selectivity. |

---

## Totals (approx)

| Layer | Keep | Add / fix | Notes |
|-------|-----:|----------:|-------|
| A Tool competence | ~27 | ~22 | Reach ≥2 per tool; include `manage_amm` ×2 |
| B Specialists (5 granted) | ~7 | ~40 | ≥8 each; fix MM grant mismatch |
| B Full-surface roles (dir + SMF) | 2 | ~16 | Job-first; not 24-tool AND |
| C General consult | ~15 | trim + few | Merge null-slug; dedupe |
| D Tick | 6 | ~5 RO | Unblocks tick recommend under RO ceiling |
| **Landing** | | **~100–120 scored intent** | Matches prior Axis A/B landing |

Overnight **routing** suite: Layers A (RO) + B (RO) + C + D (RO).  
Shortlist / mutating: destructive job cases + `t001/t002/t005` + `manage_amm` add.

---

## Implementation order

1. **Fix MM `_002`/`_003`** grant mismatch (`manage_trading_agent` → controllers/bots).
2. **Layer A top-ups** — second samples + `manage_amm` ×2.
3. **Layer B RO** for MM, SOL, DN (existing specialists with thin coverage).
4. **Layer B** XRPL + MET (currently zero agent cases).
5. **Layer D RO ticks** (unblocks tick router under default ceiling).
6. **Merge/trim GC**; Layer B dir/SMF; destructive shortlist last.
7. Wire router: specialist recommend = grant tool competence ∧ Layer B domain pass (optional code follow-up).

---

## Explicit non-goals

- Uniform N cases × 24 tools × 9 domains (no routing power).
- Mapping consult categories onto specialist domains without `agent_slug`.
- Recommending MM because shared tools like `get_market_data` passed on the full surface.
- Requiring all 24 tools green to recommend directional/smart_money (inherit-all agents — use job cases).
- Dropping `BENCH_ALLOW_MUTATING` without undo for `manage_amm`.
