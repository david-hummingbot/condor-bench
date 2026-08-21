# condor-bench

LLM benchmarking suite for the Condor automated trading assistant. Tests any model
against a fixed dataset using the same agent stack Condor runs in production
(pydantic-ai + MCP tools), and answers a specific question:

> **What is the smallest model that can do each job?**

The output is a routing config — `general_consult → qwen2.5:7b`,
`market_making_expert → qwen2.5:14b`, `tick_execution → claude-sonnet` — not just
a leaderboard.

## How runs execute

Every run goes through condor's real MCP servers against a staging
`hummingbot-api`. There is no offline or mocked mode: param correctness and
response validity only mean something against a real API — a model can pick the
right tool with plausible arguments and get an error back every single time, and
canned payloads score that 1.0.

That means a **condor checkout** and a **staging hummingbot-api** are required for
any run, and a fail-closed pre-flight refuses to start until the URL the MCP
subprocess resolves to is provably the staging one.

Setup, guard rails and the failure modes they defend against:
**[docs/STAGING.md](docs/STAGING.md)**.

## Prerequisites

- **Python 3.12+** with [uv](https://docs.astral.sh/uv/)
- **Node.js 18+** and npm (dashboard frontend)
- An **Anthropic API key** (the judge that scores answer quality)
- A **condor checkout** and a **staging hummingbot-api**

## Quick start

```bash
git clone <repo-url>
cd condor-bench
uv sync
cp .env.example .env      # set ANTHROPIC_API_KEY at minimum
make dashboard            # → http://localhost:8001
```

---

## Using the dashboard

The topbar always shows the staging URL a run will hit. Which API a run is about
to touch matters more than which model runs against it, so it is never more than a
glance away.

### Run — configure and start

1. **Staging pre-flight** at the top. Every check is listed, passing or not; a
   blocking failure disables the start button.
2. **Enable providers**. Cloud APIs take a key and a model; Ollama / LM Studio
   take a host URL and a **Load models** click; CLI agents (Claude Code, Gemini)
   just toggle.
3. **Dataset layers** — any combination of consult / tick / tools / agents.
   None selected means all four. **Core sweep** is the floor-valid iteration
   subset (same floors as the full library; not for publishing routing).
4. **Routing domain** and **category** filters.
5. **▶ Start Benchmark**.

### Live — watch progress

Progress bar, current model and case, and a results table that fills in as cases
complete. Click a row for the full response, judge reasoning, tool trace, token
counts and MCP wiring.

### Leaderboard

Best run per model by composite score.

### Matrix

Heatmap of models (columns, smallest first) × routing domains or individual MCP
tools (rows). Colour by pass rate, composite, average tokens or average cost.

Each cell carries the number of cases it rests on, because a cell built from two
cases is weaker evidence than one built from eight. Infra failures and harness
artifacts are **excluded** and counted separately rather than scored zero — a
staging outage should not read as the model getting worse. Hover a cell for the
full breakdown, including which cases were excluded and why.

### Router

The recommendations, with the criteria exposed as live controls (min pass rate,
min cases, prefer-lower-tokens). Shows what earned a recommendation, what didn't
and why, per-tool minimums, shared config-key conflicts, and a copyable Condor
config snippet.

### Results

Full run history with per-case detail.

---

## Scoring

Any component a case has no ground truth for scores `None` and its weight moves to
answer quality, so the applied weights always sum to 1.0 and a case with less
ground truth is never quietly capped below 1.0.

| Metric | Weight | How |
|---|---|---|
| **Answer quality** | 0.45 | Reference-free Claude judge over the full transcript |
| **Tool accuracy** | 0.20 | F1 on tool *names* vs `expected_tools`; `expected_no_calls` → 0 if violated |
| **Tool params** | 0.15 | Key-value subset match vs `expected_tool_params`, tolerant about representation (`"3"` ≈ `3`, `"BTC-USDT"` ≈ `["BTC-USDT"]`, `trading_pair` ≈ `trading_pairs`) and strict about meaning (`1` ≠ `true`) |
| **Live validity** | 0.10 | Did the calls actually work? Errored or empty responses score 0, plus optional `live_expected` assertions |
| **Latency** | 0.10 | `baseline_latency / test_latency`, capped at 1.0 |

A case **passes** at composite ≥ 0.70.

**Token usage is tracked but carries no weight.** A model should not fail a case
for being token-expensive. Cost is a reporting axis and an opt-in routing
tie-breaker, never a gate. Judge tokens are tallied separately — charging a 3B
model for being judged by Sonnet would make the cheap option look expensive.

### Rows that are excluded rather than scored

| Tag | Meaning |
|---|---|
| `error: infra:…` | Provider/rate limits. Excluded from averages (pre-existing behaviour). |
| `harness_artifact` | The *harness* is why this row is bad: a live run with no resolved API URL, an agent case graded against the generic Condor prompt instead of its own, or ACP auto-discovery adding tools that shift the small-model tool cut. Excluded from the routing matrix. |

That second category is the point of the whole live-mode design. A
`market_making_expert` case run without `--agent-slug` reads the chat's stores,
never finds its `pmm_config_playbook` skill, and fails — and without this tagging
the matrix would report "market_making_expert needs a bigger model" when the truth
is a misconfiguration. See [docs/STAGING.md](docs/STAGING.md#agent-scoping---agent-slug).

---

## Routing

```bash
uv run python runner.py sweep --models datasets/models.json   # benchmark every model, smallest first
uv run python runner.py matrix                                # aggregate into a matrix
uv run python runner.py route                                 # recommend
```

Per domain, the router:

1. considers only models in `datasets/models.json` — a model with no recorded
   `params_b` cannot be ranked by size, so recommending it would be guessing
   (it's reported as unrankable rather than silently dropped);
2. sorts ascending by `params_b`, cloud models last, so a local model that passes
   always beats a cloud one that also passes;
3. takes the **first** model that passes — pass rate ≥ `--min-pass-rate` (0.80),
   no destructive case below the floor, and at least `--min-cases` scored cases;
4. if nothing passes, reports the domain as unmet with the best attempt and the
   specific blocker per model. "Insufficient evidence" is distinguished from "no
   model was good enough" — the first is a dataset gap, not a model finding.

Token cost never rejects a passing model. `--prefer-lower-tokens` only reorders
models of the *same* size:

```bash
uv run python runner.py route --min-pass-rate 0.80 --prefer-lower-tokens
```

Output lands in `results/routing_recommendations.json`, including a
`condor_config_snippet` ready to apply:

```json
{
  "agents/condor/agent_key": "ollama:qwen2.5:7b",
  "agents/market_making_expert/agent_key": "ollama:qwen2.5:14b",
  "agents/_defaults/agent_key": "openrouter:anthropic/claude-sonnet-4-5"
}
```

When two domains share one config key and disagree, the larger model wins (a
shared key has to satisfy every domain using it) and the disagreement is reported
in `config_conflicts` rather than being decided by dict ordering.

---

## Baseline (optional)

Gives latency a reference point. Without it every model scores 1.0 on latency.

```bash
make baseline                    # generate with Claude Sonnet
make baseline overwrite=true     # regenerate after adding cases
```

Baselines are produced through the same code path a test run uses, so the latency
reference is measured against the same wiring the runs it scores go through.

---

## CLI

```bash
# Single model
make test MODEL=ollama:llama3.1:8b

# Filter by layer, routing domain, category
uv run python runner.py test ollama:qwen2.5:14b --layers tool,agent
uv run python runner.py test ollama:qwen2.5:14b -d market_making_expert
uv run python runner.py test ollama:qwen2.5:14b --tick-only -c risk-blocked
uv run python runner.py test anthropic:claude-sonnet-5 --consult-only
uv run python runner.py test ollama:qwen2.5:14b --tags core   # iteration subset; still floor-valid
uv run python runner.py sweep --tags core

# Force a mode for one run
uv run python runner.py test ollama:qwen2.5:14b --mode live

# Live pre-flight (exits non-zero on a blocking failure — usable as a CI gate)
uv run python runner.py staging-check

# Sweep, aggregate, recommend
uv run python runner.py sweep --max-params-b 14
uv run python runner.py sweep --only ollama:qwen2.5:14b,ollama:qwen2.5:7b
uv run python runner.py matrix --mode live
uv run python runner.py route --min-pass-rate 0.85

# Run history
make report
```

Results are saved to `results/<run-id>_<model>/` as JSON.

---

## Dataset

Three layers.

| File | Cases | What it measures |
|---|---|---|
| `datasets/consult.jsonl` | 12 | **Layer 1** — coordinator jobs: user/server/agent lookups, routine authoring, conversation-only design, journal roundtrips. Everyday portfolio/bots/history lookups live on the tool and specialist layers instead. |
| `datasets/tick.jsonl` | 8 | **Layer 1** — strategy ticks: normal, profit-taking, risk-blocked, near-capacity, error recovery, dry-run, wide-spread stand-down, learnings adherence. Agent-scoped. |
| `datasets/tools.jsonl` | 38 | **Layer 2** — focused cases per MCP tool. The whole production surface is covered; a drift test fails if a tool isn't. |
| `datasets/agents.jsonl` | 22 | **Layer 3** — routed to a specific Condor agent with its own prompt and stores. |
| `datasets/models.json` | — | Model registry with parameter counts. Drives sweep order and routing. |

Tag `core` (65 cases) still meets the routing floors and is the cheap iteration sweep — see [docs/CASE_LIST.md](docs/CASE_LIST.md). Publish routing from the full 80.

Every case carries a `risk_level` (`read_only` / `mutating` / `destructive`) and
resolves to a **routing domain**. Every risk level runs — the level drives teardown
and the destructive score floor, not whether the case is allowed; isolation comes
from pointing at a test-connector-only API. Layer 2 domains are namespaced `tool:` because a
capability bucket like "market data" is not something Condor can route to — those
verdicts come out of the matrix's per-tool axis instead.

`FRAMEWORK_IMPROVEMENTS.md` §10 proposed a per-tool competency matrix and a
capability→recommendation report as future work. Layers 2 and 3 plus the Matrix
and Router tabs are that work; the section is kept as the rationale.

---

## Suites (Condor branch A/B)

The **Suites** tab is the primary UI for durable, comparable benchmarks. Create
**Environments** (each binds a Condor checkout path, expected branch, mode) and
**Suites** (editable case list + models + attached environments). **Run all**
fans out one **subprocess worker per Environment** so Condor's `_shared` /
`ConfigManager` cannot leak across checkouts in the FastAPI process.

### Comparing two Condor branches

1. Clone Condor twice (e.g. `condor-main` on `main`, `condor-feature` on your branch).
2. In **Suites → Environments**, create one Environment per checkout (`condor_path`,
   `expected_branch`, `server_name`).
3. Create a Suite, attach both Environments, import or author cases, pick a fixed model.
4. **Run all** → Live tab shows `member_started` / `member_done` events.
5. **Compare runs** — only comparable when model, case set, and risk ceiling
   match. Latency deltas include `n`.

Suite runs are **excluded from the Matrix / Router** unless the suite sets
`include_in_matrix: true`. Every `summary.json` pins git state **and** the paths
actually loaded (`shared_py`, `config.yml`, `acp_working_dir`).

`datasets/tool_surface.json` remains a single global snapshot — tool counts for
non-default Environments may be stale relative to that checkout.

### Security

The dashboard is **trusted-local**. Environment `condor_path` is user-supplied
filesystem input the backend executes modules from (arbitrary local code by
design). Bind to localhost; do not expose the dashboard. CORS is open and there
is no auth.

### HTTP API (MCP-ready)

All suite/environment/case/run operations are HTTP CRUD — the same surface a
future bench MCP server (Openclaw, Hermes, Claude Code, Codex) should wrap.
There is no UI-only write path. See `/api/environments`, `/api/suites`,
`/api/suites/{id}/run`, `/api/compare`.

CLI (thin; dashboard remains the authoring surface):

```bash
uv run python runner.py suite list
uv run python runner.py suite run <suite-id>
```

---

## Staying in sync with condor

Two drift checks, both runnable with `make check-drift`:

| Check | Fails when |
|---|---|
| `tests/test_mcp_wiring_drift.py` | bench's MCP spawn args diverge from condor's `build_mcp_servers_for_agent()` / `_for_session()`, **or** condor's own args diverge from a pinned shape |
| `tests/test_vendored_drift.py` | `condor_compat/.../AGENT.md` no longer matches condor's system prompt |

The MCP wiring check is the one worth understanding. bench *loads* condor's
helpers rather than copying them, so bench-vs-condor comparison mostly guards the
call site — a new required parameter on condor's side would change production's
output while bench kept calling the old signature. The **pinned literal arg list**
is what turns "condor changed its MCP wiring" into a failing test someone has to
look at, instead of a change bench follows silently.

It has already earned that twice. condor deleted `build_mcp_servers_for_agent()`
and folded it into the session builder; and under SEC-095 it moved the API
credentials and bot token off argv (world-readable via `ps`) into the subprocess
`env`. A vendored copy of the spawn args would have kept putting the API password
on the command line. The pin now asserts *placement* — credentials in `env`,
coordinates in `args` — and never pins a secret's value.

condor-evals' `build_mcp_servers()` fails that pin today: no `--server-name` on
either server, no `--agent-slug` on condor. That is why its wiring was not
ported.

> **Point `CONDOR_PATH` at the checkout you mean.** More than one condor clone on
> a machine is normal — typically one on `main` and one on a feature branch with
> work in progress — and the `../condor` fallback cannot tell them apart. The
> drift checks read condor's *working tree*, so pointing them at a feature branch
> measures a condor nobody is running: it looks exactly like real drift, and its
> natural fix (re-vendor, regenerate) would sync bench to the wrong tree.
>
> `make check-drift` prints the checkout it used — path, branch, commit and
> whether it has uncommitted files. One check fails when the snapshot's commit
> isn't in that checkout's history (ancestry, not just "the object exists", so a
> `git fetch` on the wrong branch can't quietly satisfy it), and another warns
> when the checkout is dirty, since local edits correspond to no commit.

After pulling condor:

```bash
make check-drift
make tool-surface                        # re-capture if production legitimately changed
CONDOR_PATH=/path/to/condor make tool-surface
```

Never hand-edit `datasets/tool_surface.json`.

### Keeping condor_compat in sync

`condor_compat/` vendors condor's agent stack. Two files carry **deliberate**
bench-specific edits and cannot be re-vendored by copying:

- `acp/pydantic_ai_client.py` — bench-only `_TOOL_LIMITS` cap, `OPENAI_BASE_URL`
  provider detection, no-tools fallback, and per-server `cwd` (bench launches
  condor's servers with `uv run`, which only resolves inside the condor project —
  condor doesn't need this because its own process is already there)
- `agents/prompts.py` — condor model imports replaced with `Any`

`acp/client.py` carries the usage types re-vendored from production
(`UsageEvent`, `fold_usage_event`, the ACP usage parsers), and
`pydantic_ai_client.py` carries `estimate_cost_usd()` / `_fold_run_usage()`.
Re-syncing those is a manual diff-and-review against condor, keeping the local
edits. `agents/condor/AGENT.md` is a plain body copy (YAML frontmatter stripped)
and is the one vendored file the drift test can check automatically.

---

## Consolidation with condor-evals

condor-bench is the single harness. What was taken from condor-evals:

| Asset | Action |
|---|---|
| `expected_tool_params` scoring | Ported → `metrics/tool_params.py`, minus DeepEval |
| `condor_tasks.json` cases | Migrated → `datasets/tools.jsonl` and `datasets/agents.jsonl` (tagged `ported:<id>`) |
| `load_assistant_prompt()` | Ported → `bench/client.py`, reading from the condor checkout |
| Dashboard matrix UI | Reimplemented → the Matrix tab |
| Live MCP wiring | **Not ported** — already drifted from production |
| DeepEval | **Not ported** — condor-bench uses a direct Claude judge |

One case was dropped rather than migrated: `condor_011` expected a
`setup_connector` tool that production does not expose.

---

## Dev mode (hot-reload)

```bash
make dashboard-dev
```

Backend: http://localhost:8001 · Vite dev server: http://localhost:5173

---

## Project layout

```
condor-bench/
├── bench/
│   ├── client.py         # LLM client: MCP wiring, tool traces, token usage
│   ├── mcp_provider.py   # MCP configs from condor's real wiring
│   ├── staging_health.py # fail-closed staging pre-flight
│   ├── cleanup.py        # post-case teardown for mutating cases
│   ├── dataset.py        # 4 case types, risk levels, routing domains
│   ├── scorer.py         # composite scoring
│   ├── matrix.py         # model × domain/tool aggregation + model registry
│   ├── routing.py        # smallest-passing-model recommendations
│   ├── baseline.py       # baseline latency store
│   └── reporter.py       # save/load runs, usage roll-ups
├── metrics/
│   ├── answer_quality.py # reference-free Claude judge
│   ├── judge.py          # judge client + separate token accounting
│   ├── tool_accuracy.py  # F1 on tool names
│   ├── tool_params.py    # argument correctness
│   ├── live_validity.py  # did the calls actually work
│   └── latency.py        # baseline-relative latency
├── condor_compat/        # vendored condor agent stack
│   ├── acp/              #   pydantic-ai client, ACP client, JSON-RPC peer
│   └── agents/           #   tick prompt builder + condor/AGENT.md (body only)
├── datasets/             # 4 case files + tool_surface.json + models.json
├── suites/               # Environments + suite cases (file-backed, dashboard-edited)
│   └── environments/
├── scripts/
│   ├── snapshot_tool_surface.py
│   ├── register_bench_server.py
│   ├── sync_case_prompts.py
│   ├── suite_worker.py           # subprocess member runner (one Condor checkout)
│   └── validate_environment.py   # isolated checkout probe
├── dashboard/            # FastAPI + React (Suites/Run/Live/Prompt/Leaderboard/Matrix/Router/Results)
├── docs/STAGING.md       # staging setup and guard rails
├── baseline/             # baseline latency records (tracked — shared reference)
├── results/              # benchmark run outputs (git-ignored)
├── config.py             # paths, score weights, thresholds
└── runner.py             # CLI: baseline, staging-check, test, sweep, matrix, route, report
```
