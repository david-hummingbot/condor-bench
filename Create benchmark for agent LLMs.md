# Create benchmark for agent LLMs

Due: July 13, 2026
Assignee: Michael David Alvero
Status: In Progress
Last edited time: July 10, 2026
Project: Condor (https://app.notion.com/p/Condor-338db4591165488d9da5e885b4cd0322?pvs=21)
Project Status: In Progress
Last edited by: Michael Feng

# LLM Agent Benchmarking — Implementation Status

> **Goal:** Build a repeatable benchmark to evaluate how different LLM models perform when operating as agents inside Condor, scored against a fixed dataset of consult and tick tasks using the same agent stack (pydantic-ai + MCP tools) as production.
>

**Repo:** `condor-bench` — working harness, datasets, scoring, CLI, and dashboard are implemented. See also `README.md` for operator docs.

---

## 1. Overview

| Field | Detail |
| --- | --- |
| **Project Name** | Condor LLM Agent Benchmark (`condor-bench`) |
| **Status** | 🟢 Harness v1 implemented — expanding coverage & analysis |
| **Owner** | Michael David Alvero |
| **Target Start** | June 2026 |
| **Target Completion** | July 13, 2026 |

### Problem Statement

As Condor supports AI agents for consult (Q&A / configuration) and tick (strategy loop) workflows, there was no systematic way to evaluate which LLM model performs best. Different models may excel at different task types — this benchmark surfaces that signal clearly and repeatably against a fixed dataset.

### Success Criteria

| Criterion | Status |
| --- | --- |
| Defined task list with clear scoring criteria | ✅ Consult (23) + Tick (6) cases in JSONL |
| At least 3 LLM models evaluated | ✅ Multi-provider support; runs recorded under `results/` |
| Reproducible, automated scoring | ✅ Composite scorer (quality + tools + latency) |
| Results report comparing models | ✅ CLI `report` + dashboard Leaderboard / Runs |

---

## 2. Phase 1 — Research: Evaluation Frameworks & Evals

**Goal:** Identify existing frameworks so we don't reinvent the wheel.

### 2.1 Decision (adopted)

**Hybrid custom harness** — not a full third-party eval platform.

| Choice | Detail |
| --- | --- |
| **Agent runtime** | Same stack as Condor: pydantic-ai + MCP tools (`condor_compat/`) |
| **Answer quality** | Reference-free Claude judge (`metrics/answer_quality.py`) — accuracy, completeness, safety, actionability |
| **Tool correctness** | F1 on tool *names* vs dataset `expected_tools` / `expected_tool_calls` |
| **Latency** | Relative to Sonnet baseline: `min(1.0, baseline / test)`, floor 0.1 |
| **Orchestration** | Custom CLI (`runner.py`) + FastAPI/React dashboard |

DeepEval is a dependency but answer quality calls Claude directly (avoids GEval's internal OpenAI routing). Mock MCP servers mirror production tools so runs are deterministic and offline-safe for tool responses.

### 2.2 Frameworks considered (rejected in favor of the above)

Evaluated and passed on: LangSmith, Braintrust, RAGAS (not RAG-centric), PromptFoo, OpenAI Evals — none fit an agent-tool-call benchmark scored against a fixed dataset well enough to justify the integration cost. AgentBench / Tau-bench informed task design (multi-turn, tool-call ground truth) but weren't adopted as the runtime.

### 2.3 Scoring approaches in use

| Approach | Where used |
| --- | --- |
| **LLM-as-judge (rubric)** | Answer quality — 0.0–1.0 vs Condor domain criteria |
| **Structured / set match (F1)** | Tool accuracy — expected vs actual tool names |
| **Execution-relative** | Latency vs baseline model timing |
| **Pass threshold** | Case passes when composite ≥ 0.70 |

---

## 3. Phase 2 — Task Definition

**Goal:** Testable cases with clear input, expected tools (where applicable), and scoring via the composite metric.

### 3.1 Case schema

**Consult** (`datasets/consult.jsonl`):

```
id, type=consult, category, question, expected_tools[], turns[], mock_tools{}, tags[]
```

**Tick** (`datasets/tick.jsonl`):

```
id, type=tick, category, scenario_name, agent_instructions, strategy_instructions,
config, risk_state, core_data, learnings, summary, recent_decisions, tick_number,
mock_tools{}, expected_tool_calls[], expected_no_calls[], tags[]
```

Scoring is not per-task method selection — every case uses the same composite scorer. Cases without tool ground truth (custom prompts) redistribute the tool weight into answer quality.

---

### 3.2 Consult cases — Condor Agent Q&A / configuration (23)

> *Consult mode: user questions against the Condor system prompt + MCP tools.*

| Task ID | Category | Description | Expected tools |
| --- | --- | --- | --- |
| c001 | concepts | Grid vs CLMM — when to choose which | — |
| c002 | troubleshooting | Grid running 4h with no fills | — |
| c003 | risk | Max drawdown stop — restart guidance | — |
| c004 | configuration | $5k capital — sizing & risk spread | — |
| c005 | strategy | Trend-following on ETH/USDT setup | — |
| c006 | concepts | What happens during a strategy tick | — |
| c007 | risk | SOL altcoin grid risk limits | — |
| c008 | concepts | Consult mode vs loop mode | — |
| c009 | analysis | P&L attribution / realized vs unrealized | `manage_executors` |
| c010 | strategy | When grid vs trend/DCA | — |
| c011 | troubleshooting | Local Ollama agent never calls tools | — |
| c012 | strategy | 30-day BTC DCA configuration | — |
| c013 | configuration | Connect Binance (multi-turn, API keys) | `manage_servers` |
| c014 | strategy | Grid setup wizard (multi-turn) | `get_market_data` |
| c015 | troubleshooting | Losing grid diagnosis (multi-turn) | `get_market_data`, `manage_executors` |
| sc001 | strategy-creation | Create BTC grid executor ($800, ±2%, 10 levels) | `get_market_data`, `manage_executors` |
| sc002 | strategy-creation | Create ETH DCA executor | `manage_executors` |
| sc003 | strategy-creation | XRPL → Binance XRP grid (multi-turn) | `get_portfolio_overview`, `get_market_data`, `manage_executors` |
| sc004 | strategy-creation | Deploy PMM controller on Hummingbot bot | `manage_bots`, `manage_controllers` |
| rb001 | routine-builder | Skill vs. routine — when to write one | — |
| rb002 | routine-builder | Diagnose a silently-failing scheduled routine | `manage_routines` |
| rb003 | routine-builder | Read `routine_builder` skill, list, then create a routine | `manage_skill`, `manage_routines` |
| rb004 | routine-builder | Create + run a drawdown-alert routine (multi-turn) | `manage_routines` |

*Multi-turn cases: c013, c014, c015, sc003, rb004.*

---

### 3.3 Tick cases — Trading Agent loop (6)

> *Tick mode: strategy tick prompt with risk state, market context, and expected tool behavior.*

| Task ID | Category | Scenario | Expected tools | Must not call |
| --- | --- | --- | --- | --- |
| t001 | normal | BTC Grid — first tick, no position | `get_market_data`, `manage_executors`, `trading_agent_journal_write` | — |
| t002 | profit-management | ETH long grid — overbought, high PnL | `manage_executors`, `trading_agent_journal_write` | — |
| t003 | risk-blocked | Max drawdown exceeded — blocked | `trading_agent_journal_write` | `manage_executors` |
| t004 | near-limit | Near executor limit — volatile market | `get_market_data`, `trading_agent_journal_write` | — |
| t005 | error-recovery | SOL executor schema error recovery | `manage_executors`, `trading_agent_journal_write` | — |
| t006 | dry-run | ETH dry-run — observation only | `get_market_data` | `manage_executors` |

---

### 3.4 Routine Builder

**Status:** ✅ Implemented — 4 cases (rb001–rb004) added to `datasets/consult.jsonl` under category `routine-builder`, covering the `manage_routines` / `manage_skill` tools and the `routine_builder` playbook (concepts, diagnosing a failing routine, skill-guided creation, and a multi-turn create-then-run case). Verified end-to-end against `claude-haiku-4-5` — mocks and scoring work correctly, though answer-quality is noticeably lower here than other categories (models tend to fabricate diagnostic detail like log lines or "current" values instead of admitting they can't inspect routine internals) — worth another look once more models have been run.

---

## 4. Phase 3 — Benchmark Design & Infrastructure

**Status:** ✅ Implemented in `condor-bench`.

### 4.1 Models / providers supported

| Provider | Kind | Notes |
| --- | --- | --- |
| Anthropic | Cloud | e.g. `claude-sonnet-4-6`, `claude-opus-4-8`, `claude-haiku-4-5` |
| OpenAI | Cloud | e.g. `gpt-4o`, `gpt-4o-mini`, `o1-mini`, `o3-mini` |
| OpenRouter | Cloud | Gemini, Llama, Qwen, Mistral, etc. |
| Groq | Cloud | Fast hosted open models |
| Ollama | Local | Host URL + model list from `/api/tags` |
| LM Studio | Local | OpenAI-compat local server |
| Custom (OpenAI-compat) | Local / gateway | Arbitrary base URL |
| Claude Code / Gemini CLI | Agent CLI | Uses tools on PATH (ACP path) |

Baseline default: `anthropic:claude-sonnet-4-6`. Judge default: `claude-sonnet-4-6` (requires `ANTHROPIC_API_KEY`).

### 4.2 Benchmark harness — components

| Component | Path | Status |
| --- | --- | --- |
| **Task runner** | `runner.py`, dashboard `/api/runs` | ✅ |
| **Prompt / agent client** | `bench/client.py` + `condor_compat/` | ✅ |
| **Dataset loaders** | `bench/dataset.py` | ✅ |
| **Mock MCP tools** | `mock_mcp/` | ✅ |
| **Response recorder** | `bench/reporter.py` → `results/<run>/` | ✅ |
| **Scorer** | `bench/scorer.py` + `metrics/` | ✅ |
| **Baseline store** | `bench/baseline.py` → `baseline/` | ✅ |
| **Report generator** | CLI `report` + dashboard Leaderboard/Runs | ✅ |
| **Custom prompt runs** | Dashboard Custom Prompt tab | ✅ |

### 4.3 Evaluation metrics (implemented)

| Metric | Weight | Description |
| --- | --- | --- |
| **Answer quality** | 50% | Claude judge, 0–1, Condor domain rubric (no baseline response required) |
| **Tool accuracy** | 30% | F1 of actual vs expected tool names |
| **Latency score** | 20% | `baseline_latency / test_latency`, capped at 1.0, floor 0.1 |

**Composite** = 0.5 × quality + 0.3 × tools + 0.2 × latency

**Pass:** composite ≥ 0.70

When `expected_tools` is absent (custom prompts), tool weight is folded into answer quality (80% quality + 20% latency).

### 4.4 How to run

```bash
uv sync
cp .env.example .env   # set ANTHROPIC_API_KEY at minimum

make baseline          # optional latency reference (Sonnet)
make dashboard         # http://localhost:8001

# CLI
make test MODEL=anthropic:claude-haiku-4-5-20251001
make test MODEL=ollama:llama3.1:8b
make report
```

---

## 5. Phase 4 — Execution & Analysis

### 5.1 Execution steps

- [x] Run task suite across models via CLI and dashboard
- [x] Persist per-case JSON + run summaries under `results/`
- [ ] Manual review of edge cases and borderline scores
- [ ] Flag tasks where judge scoring is ambiguous or noisy
- [ ] Formal multi-run consistency study (3× per model)

### 5.2 Analysis

- [x] Per-model averages: composite, quality, tools, latency (CLI + dashboard)
- [x] Leaderboard (best run per model)
- [ ] Per-category / per-task heatmap published as a report
- [ ] Cost vs. performance quadrant (token/cost tracking not yet first-class)
- [ ] Recommendation on default model per Condor agent mode (consult vs tick)

### 5.3 Deliverables

- [x] Results dataset (raw scores in `results/`)
- [ ] Analysis report with model recommendations per agent mode
- [ ] Recommendation on default production model(s) for Condor

---

## 6. Open Questions

- [ ] How do we handle non-determinism? (temperature, multiple runs, variance reporting)
- [ ] Should categories or difficulty weight the final leaderboard score?
- [ ] Is Claude-as-judge sufficient, or do we need spot human review for rubric tasks?
- [ ] What's the minimum composite / pass-rate to call a model “production-ready”?
- [ ] Should the benchmark run in CI for regression testing?
- [ ] How frequently should we re-run as models are updated?
- [ ] When do we add a dedicated Routine Builder dataset?
- [ ] Should `expected_no_calls` be scored explicitly (today tool F1 uses expected calls only)?

---

## 7. Milestones

| # | Milestone | Owner | Status |
| --- | --- | --- | --- |
| 1 | Research phase complete + framework decision | Michael | ✅ Done — hybrid custom + Claude judge |
| 2 | Task lists for consult + tick agents | Michael | ✅ Done — 23 consult (incl. Routine Builder) + 6 tick |
| 3 | Benchmark harness v1 built and tested | Michael | ✅ Done — CLI, scoring, mock MCP, dashboard |
| 4 | First full benchmark runs complete | Michael | 🟡 In progress — multiple runs in `results/` |
| 5 | Analysis report + production model recommendation | Michael | 🔲 Not started |

---

## 8. Project layout (current)

```
condor-bench/
├── bench/                 # Client, dataset, scorer, baseline, reporter
├── metrics/               # Answer quality, tool accuracy, latency, judge
├── mock_mcp/              # Mock Hummingbot + Condor MCP tools
├── condor_compat/         # Vendored Condor pydantic-ai / ACP / prompts
├── datasets/              # consult.jsonl, tick.jsonl
├── dashboard/             # FastAPI backend + React frontend
├── baseline/              # Latency baselines (git-ignored content)
├── results/               # Run outputs (git-ignored)
├── config.py              # Paths, weights, baseline/judge models
├── runner.py              # Typer CLI: baseline | test | report | dashboard
└── Makefile
```

---

*Last updated: July 10, 2026 — synced with `condor-bench` implementation, added Routine Builder cases (rb001–rb004)*
