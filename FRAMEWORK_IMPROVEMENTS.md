# Condor Agent Framework — Improvement Recommendations

**Goal:** Raise answer/tool accuracy, improve end-to-end agent performance, cut token usage, and make Condor usable with small LLMs (**4B–9B**) without constant failure, fabrication, or request-limit blowups.

**Evidence base:** condor-bench runs (consult + tick + strategy-creation + routine-builder), including strong models (Opus / GPT-class ≈ 0.70–0.76 composite) and weak ones (Qwen 3.5 9B / Gemma ≈ 4–12B ≈ 0.45–0.50, with frequent `request_limit` / token-limit infra fails). Prompt-only fixes help; remaining gaps are structural.

---

## 1. Design principles (small-model first)

Treat 4B–9B as the primary design constraint. If a flow works on a 7B tool-calling model under a tight tool budget, larger models will generally work better—not the reverse.

| Principle | Why |
|-----------|-----|
| **Prefer code over chain-of-tools** | Small models lose schema fidelity after 2–3 tool calls. One validated high-level op beats five `manage_*` steps. |
| **Ground every claim** | Small models invent “deployed” / PnL numbers when uncertain. Session must separate *narrative* from *tool facts*. |
| **Tiny, progressive context** | Full AGENT.md + all MCP schemas + skills indexes explode context. Load only what the current task needs. |
| **Fail closed on mutations** | Never allow success language without a matching tool result. Confirmation UI must run *before* the claim, not after a fake one. |
| **Specialists with narrow tools** | Coordinator + domain agents with 3–6 tools each beat one mega-prompt with every Hummingbot action. |

---

## 2. Accuracy — stop fabrication and wrong actions

### 2.1 Session grounding layer (P0)

**Problem:** Even Opus invents successful creates (`grid-btc-001`, “already live”) on turn 1, then insists the executor exists after confirm. Bench judge correctly hammers quality.

**Recommendation:**

1. After each assistant turn, persist a **canonical fact set**: `{tool, args_digest, result}` for every MCP call.
2. Inject into the *next* model turn a short block, e.g.:

   ```text
   [GROUND TRUTH — only assert these facts]
   - get_market_data → mid=65000, rsi=48
   - (no manage_executors create this session yet)
   ```

3. Optional guard: if outbound text matches patterns (`deployed successfully`, `executor is live`, `created grid-…`) without a matching create result → strip claim and append “No create has succeeded yet; call the tool or say you have not deployed.”

**Touches:** ACP/pydantic-ai stream handlers, consult/chat session wrappers (not only tick snapshots).

### 2.2 Propose → confirm → execute (P0)

**Problem:** Telegram confirmation (`handlers/agents/confirmation.py`) gates *real* MCP calls, but models dodge the gate by narrating success without calling tools.

**Recommendation:**

1. For dangerous actions (`manage_executors` create/stop, `place_order`, swaps, CLMM), require a **structured proposal** (typed JSON / tool `propose_create_grid`) that does *not* mutate.
2. Framework renders the confirm UI from that proposal.
3. On approve, framework executes the real tool and posts a **result card** (framework-authored text), not free-form “I deployed…”.

This moves “did it succeed?” out of the LLM’s mouth for money-moving ops.

### 2.3 Fix dead specialist routing (P0)

**Problem:** `assistants/condor/AGENT.md` steers deploys to `executor_manager`, but shipped agents are only `routine_builder` and `market_making_expert`. Condor free-forms grid/DCA creates and fabricates.

**Recommendation:**

- **Either** ship `agents/executor_manager/` with a short AGENT.md, narrow tool allowlist (`get_market_data`, `get_portfolio_overview`, `manage_executors`, optionally bots/controllers), and hard rule: no success without tool result.
- **Or** remove/rewrite the `executor_manager` references so routing matches reality (`market_making_expert` for PMM; raw high-level tools for grid/DCA).

Keep `[AGENTS]` index generation in sync with disk (fail loud if AGENT.md cites a missing slug).

### 2.4 Force tool-first on live-state questions (P1)

**Problem:** Models write excellent PnL / portfolio essays (`c009`) with **zero** `manage_executors` calls.

**Recommendation:** Ship small skills that *require* a read before answer:

| Skill | Trigger | First step |
|-------|---------|------------|
| `pnl_report` | “PnL”, “realized vs unrealized”, performance | `manage_executors` (performance / list) |
| `diagnose_executor` | “losing”, “no fills”, “stuck grid” | `get_market_data` + `manage_executors` |
| `portfolio_snapshot` | “how much”, “balances”, sizing against capital | `get_portfolio_overview` |

Condor should `manage_skill(read)` then follow; coordinator mustn’t answer from memory when user refers to *their* state.

### 2.5 Tick: bind `controller_id` server-side (P1)

**Problem:** Error-recovery ticks (`t005`) thrash on schema / `controller_id` placement; small models hit request limits mid-retry.

**Recommendation:**

- For loop agents, wrap `manage_executors(action=create)` so `controller_id=agent_id` is always injected server-side (don’t rely on the LLM).
- On schema / missing-field errors, return a **minimal valid example payload** in the tool error body (not only a field list).
- Cap retries at one (already in prompt); after fail, journal and stop—don’t loop inventing shapes.

---

## 3. Performance — fewer steps, clearer paths

### 3.1 High-level mutation tools (P0 for small models)

**Problem:** Creating a grid/DCA/PMM today is multi-call (`market` → `portfolio` → `manage_executors` / `manage_bots` + `manage_controllers`). Multi-step tool use is exactly where 4B–9B collapse.

**Recommendation:** Add validated MCP convenience ops:

| Tool | Responsibility |
|------|----------------|
| `create_grid_executor` | Pair, range or ±%, levels, quote amount, connector → create |
| `create_dca_executor` | Pair, amount, interval, max orders, stop → create |
| `deploy_pmm_controller` | Bot name, pair, spread, size → bots + controllers |
| `stop_executor` | ID + reason → stop |

Implementation: thin wrappers over existing Hummingbot APIs with Pydantic validation and clear error strings. LLM calls **one** tool with business params.

### 3.2 Narrow tool surfaces per role (P1)

**Problem:** Loading the full Hummingbot + Condor MCP catalog burns tokens and confuses small models.

**Recommendation:**

| Role | Approximate tool set |
|------|----------------------|
| Condor (coordinator) | consult, delegate, memory, skill, notify, get_user_context, portfolio/market read, routine **run/list** only |
| executor_manager | market, portfolio, manage_executors (+ high-level create_* ) |
| routine_builder | manage_routines*, manage_skill |
| market_making_expert | market, portfolio, bots, controllers, executors (as today) |
| Tick agent | market, manage_executors, journal_write, notify, memory/skill/routines as needed |

Use existing agent `tools:` frontmatter + MCP filter strictly. Do not expose `place_order` / gateway mutate to the coordinator if specialists own them.

### 3.3 Deterministic tick providers (already strong — extend)

TickEngine already precomputes executors/positions. Extend providers so the LLM rarely needs exploratory reads on tick 1:

- Candles / RSI / spread summary in `[CORE DATA]` when strategy requests them.
- Last create-error + schema snippet when previous tick failed (feeds recovery without a blind schema dance).

---

## 4. Token usage — cut context aggressively

### 4.1 Progressive disclosure of AGENT.md (P0)

**Problem:** Full Condor AGENT.md (routing, memory, skills, executors, formatting) is prepended every consult. Small local models pay this on every turn and then hit tool-loop limits.

**Recommendation:** Split into:

1. **Core** (~30–50 lines): identity, safety, no-fabricate, confirm-vs-execute, keys → dashboard.
2. **Modules** loaded only when needed: routing detail, memory API, skills API, executor glossary, routine rules.

Load modules via skill read or a `load_playbook(name)` tool—not every turn.

### 4.2 Compact tool schemas for small models (P0)

**Recommendation:**

- Maintain **short** and **full** JSON schemas per tool. For `agent_key` matching ollama/lmstudio/known small cloud IDs, register the short schema (required fields + 1-line descriptions only).
- Drop rarely used actions from the default view; expose via `manage_executors(action="help")` / schema fetch when needed.

### 4.3 Slim skill / agent indexes (P1)

Inject **one-line** `[SKILLS]` / `[AGENTS]` indexes (name + when_to_use), not paragraphs. Full body only after `manage_skill(read)` / consult.

### 4.4 Truncate tool results before re-feeding the model (P1)

Large portfolio / order-book / performance dumps swamp 4B–9B context. Cap and summarize in the MCP layer (e.g. top balances, executor count, PnL totals) with `detail=full` opt-in.

### 4.5 Tick prompt budget (P1)

Audit `build_tick_prompt`: keep risk + core data + last N decisions; omit empty sections; avoid duplicating preload tool lists for pydantic-ai (tools already bound). Target a hard budget (e.g. ≤ 4–6k tokens of framework context before strategy body).

---

## 5. Small LLMs (4B–9B) — make Condor “just work”

### 5.1 Explicit model tiers (P0)

Introduce tiers in config (example):

| Tier | Examples | Behavior |
|------|----------|----------|
| `S` | 4B–9B local / small cloud | High-level tools only; short schemas; core AGENT.md; max 3–5 tool calls / turn; no multi-hop consult unless necessary |
| `M` | mid-size | Full tools; specialists encouraged |
| `L` | Opus / Sonnet / GPT large | Full routing; longer recovery |

Persist tier on `agent_key` prefix or a `model_tier` field on Agent/session.

### 5.2 Tool-call budget & soft stop (P0)

Bench shows Qwen/`request_limit of 50` mid-case → aq=0.

**Recommendation:**

- Cap internal tool iterations per user message (e.g. 5 for tier S, 10 for M).
- On budget exhaust: return best-effort text + “Stopped to save tokens; say continue to resume.”
- Don’t treat provider limit errors as model stupidity in product UX—retry once with colder context.

### 5.3 Prefer specialist consult for hard domains on tier S (P1)

Coordinator on 7B should not write routines or invent PMM configs. Force:

- Routine create/edit/debug → `consult(routine_builder)` (already in AGENT.md; enforce in router).
- Grid/DCA create → high-level tool or `executor_manager`.
- PMM deploy → `delegate(market_making_expert)` when long.

If local backend can’t run the specialist, show a clear “this task needs a stronger/specialty agent” instead of attempting with empty tool skill.

### 5.4 Template replies for advisory concepts (P2)

For conceptual questions (grid vs CLMM, consult vs loop, skill vs routine), optionally serve **retrieved short answers** (RAG / static cards) and let the small model only personalize. Saves tokens and boosts accuracy on the “concepts” bench category small models already sometimes pass.

### 5.5 Default safer agent_key for constrained installs (P2)

Document recommended local stacks (e.g. Qwen3 8B / Gemma tool-tuned builds) with Condor-tested `temperature`, tool-call format, and tier `S` defaults. Refuse known non–tool-calling models for loop agents (align with existing troubleshooting guidance).

---

## 6. Suggested implementation phases

### Phase A — Accuracy foundations (1–2 weeks)

1. Session grounding injection + optional anti-fabrication guard.
2. Align AGENT.md with shipped agents (add or remove `executor_manager`).
3. Propose → confirm → execute for `manage_executors` create/stop (and place_order).
4. Server-side `controller_id` on tick creates + richer schema errors.

**Exit criteria:** Strategy-creation bench cases stop claiming success without tools; t005 recovery succeeds more often on mid+ models.

### Phase B — Small-model path (2–3 weeks)

1. High-level `create_grid` / `create_dca` / `deploy_pmm` tools.
2. Model tier `S`/`M`/`L` with short schemas + core prompt only.
3. Tool-call budgets and summarized tool results.
4. Progressive AGENT.md modules.

**Exit criteria:** A 7B–9B tier-S model completes a fully specified “create BTC grid $800 ±2% 10 levels” flow without fabrication or request_limit kill; advisory concepts stay ≥ pass rate on bench.

### Phase C — Performance & scale (ongoing)

1. Expand tick providers (market snapshot, last error).
2. Skills for PnL / diagnose / portfolio.
3. Index hygiene and loud errors for missing consult targets.
4. Continuous condor-bench gate on a fixed small-model matrix (e.g. Qwen 3.5 9B + one 4B tool model).

---

## 7. Success metrics

Track on condor-bench + production telemetry:

| Metric | Target direction |
|--------|------------------|
| Composite / pass rate (full suite) | ↑ especially `strategy-creation`, `routine-builder`, `error-recovery` |
| Fabrication rate (judge or heuristic: success claim w/o tool) | ↓ toward ~0 on mutations |
| Tokens / successful create flow | ↓ (one high-level call vs 4–6 raw calls) |
| Tool calls / tick (median) | ↓ or stable with ↑ task success |
| Infra fail rate (`request_limit`, token limit) on 4B–9B | ↓ sharply |
| Pass rate on pinned 7B–9B run | Primary gate for “seamless” small-model support |

---

## 8. What not to do

- **Don’t** keep growing AGENT.md as the main fix—Opus still fabricates under the current long prompt.
- **Don’t** expose every Hummingbot action to every model “for power users” by default; power users can opt into tier `L`.
- **Don’t** rely on “never fabricate” text alone without grounding or propose/confirm.
- **Don’t** instruct consult to missing agent slugs.

---

## 9. Related code / docs

| Area | Location |
|------|----------|
| Tick loop / snapshots | `condor/agents/engine.py`, `prompts.py` |
| Confirm gate | `handlers/agents/confirmation.py`, `_shared.py` (`is_dangerous_tool_call`) |
| Consult / delegate | `condor/agents/consult.py`, `delegate.py` |
| Coordinator prompt | `assistants/condor/AGENT.md` |
| Specialists | `agents/routine_builder/`, `agents/market_making_expert/` |
| Executor MCP | `mcp_servers/hummingbot_api/tools/executors.py` |
| Bench evidence | sister repo `condor-bench` (`datasets/`, `results/`) |

---

*This document is a roadmap, not a committed sprint plan. Prioritize Phase A before expanding prompts or adding more raw MCP tools.*
