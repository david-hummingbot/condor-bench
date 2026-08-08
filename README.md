# condor-bench

LLM benchmarking suite for the Condor automated trading assistant. Tests any model against a fixed dataset of consult and tick cases using the same agent stack (pydantic-ai + MCP tools) that Condor uses in production.

## Prerequisites

- **Python 3.12+** with [uv](https://docs.astral.sh/uv/) installed
- **Node.js 18+** and npm (for the dashboard frontend)
- An **Anthropic API key** (required for the judge LLM that scores responses)

## Quick start

### 1. Clone and install

```bash
git clone <repo-url>
cd condor-bench
uv sync
```

### 2. Configure API keys

```bash
cp .env.example .env
```

Edit `.env` and set at minimum:

```env
ANTHROPIC_API_KEY=sk-ant-...        # Required — used by the judge to score responses
```

Add any other keys for the models you want to benchmark:

```env
OPENROUTER_API_KEY=sk-or-...        # For OpenRouter models
OPENAI_API_KEY=sk-...               # For OpenAI models
```

Keys for Ollama and LM Studio are configured directly in the dashboard (no API key needed, just a host URL).

### 3. Launch the dashboard

```bash
make dashboard
```

Open **http://localhost:8001** in your browser.

That's it. Everything else is done from the UI.

---

## Using the dashboard

### Run tab — configure and start a benchmark

1. **Enable providers** — toggle the providers you want to test. Each enabled provider expands to show its settings.
2. **Cloud APIs** (Anthropic, OpenAI, OpenRouter, Groq): enter your API key and pick a model from the dropdown.
3. **Local models** (Ollama, LM Studio): enter the host URL (e.g. `http://192.168.1.10:11434`), click **Load models** to fetch the available model list, then pick one.
4. **CLI agents** (Claude Code, Gemini CLI): just toggle — no key needed, uses the CLI tools already on your PATH.
5. Set optional filters (consult-only, tick-only, category).
6. Click **▶ Start Benchmark**.

### Live tab — watch progress

Switches automatically when a run starts. Shows:
- Progress bar (cases completed / total)
- Current model and case being evaluated
- Results table that updates in real time — click any row to expand the full response and judge reasoning

Cancel a running benchmark with the **Cancel** button.

### Leaderboard tab

Bar chart + score table showing the best run per model, sorted by composite score.

### Runs tab

Full history of completed runs. Click a run in the sidebar to see per-case scores, responses, and judge reasoning.

---

## Scoring

Each case is scored on three dimensions:

| Dimension | Weight | How it's measured |
|-----------|--------|-------------------|
| **Answer quality** | 50% | Reference-free judge (Claude evaluates response / multi-turn transcript — no baseline needed) |
| **Tool accuracy** | 30% | F1 on tool names vs non-empty `expected_tools`. Empty `[]` skips this metric (weight → quality). `expected_no_calls` → 0 if violated |
| **Latency** | 20% | `baseline_latency / test_latency`, capped at 1.0 — only meaningful after running a baseline |

**Composite** = 0.5 × quality + 0.3 × tools + 0.2 × latency (or 0.8 × quality + 0.2 × latency when no required tools)

A case **passes** when composite ≥ 0.70.

Infra failures (token/request limits) are tagged `error: infra:…` and excluded from summary averages.

---

## Baseline (optional)

The baseline provides a latency reference point so slower models score lower on the latency dimension. Without it, every model gets 1.0 for latency (not useful for comparison).

To generate baselines with Claude Sonnet:

```bash
make baseline
```

Run this once. After that, every benchmark run scores latency relative to Sonnet's speed.

To regenerate (e.g. after adding new cases):

```bash
make baseline overwrite=true
```

---

## CLI usage

The dashboard covers the common workflow, but you can also run benchmarks from the terminal:

```bash
# Run all cases against a model
make test MODEL=anthropic:claude-haiku-4-5-20251001
make test MODEL=ollama:llama3.1:8b
make test MODEL=openrouter:google/gemini-flash-2.0

# Consult cases only
uv run python runner.py test anthropic:claude-sonnet-5 --consult-only

# Tick cases only, filter by category
uv run python runner.py test ollama:qwen2.5:14b --tick-only -c risk-blocked

# Print a summary table of all runs
make report
```

Results are saved to `results/<run-id>_<model>/` as JSON.

---

## Dataset

| File | Cases | Description |
|------|-------|-------------|
| `datasets/consult.jsonl` | 15 | "Everyday usage" cases — the simple, high-frequency status-lookup questions real users ask most (portfolio balance, active server, market price, open orders, bots, executors, trade history, skills, model in use, etc.). Each resolves to exactly one tool call; single-turn. |
| `datasets/tick.jsonl` | 6 | Strategy tick cases: normal execution, profit-taking, risk-blocked, near-capacity, error recovery, dry-run observation. |

See `FRAMEWORK_IMPROVEMENTS.md` §10 for the roadmap toward broader per-tool/per-agent coverage (specialist-agent tests, delegation-flow tests, per-tool competency matrix).

---

## Staying in sync with condor

Benchmarks run against the mocks in `mock_mcp/`, not the real MCP servers. When
condor's servers change and the mocks don't follow, every tool-accuracy score
afterwards is measured against a surface production no longer has — silently.

`datasets/tool_surface.json` pins the real surface, and two test modules compare
against it:

| Check | Fails when |
|---|---|
| `tests/test_tool_surface_drift.py` | a mock is missing a production tool, exposes one production doesn't have, or ignores a param production requires; or a dataset `expected_tools` entry names a tool that doesn't exist |
| `tests/test_vendored_drift.py` | `condor_compat/.../AGENT.md` no longer matches condor's system prompt (skipped without a condor checkout) |

After pulling condor:

```bash
make check-drift
```

If it fails because production legitimately changed, re-capture the surface and
update the mocks to match — never hand-edit the snapshot:

```bash
make tool-surface                        # assumes ../condor
CONDOR_REPO=/path/to/condor make tool-surface
```

### Keeping condor_compat in sync

`condor_compat/` vendors condor's agent stack. Two files carry **deliberate**
bench-specific edits and so cannot be re-vendored by copying:

- `acp/pydantic_ai_client.py` — bench-only `_TOOL_LIMITS` cap and
  `OPENAI_BASE_URL` provider detection
- `agents/prompts.py` — condor model imports replaced with `Any`

Re-syncing those is a manual diff-and-review against condor, keeping the local
edits. `agents/condor/AGENT.md` is a plain body copy (YAML frontmatter
stripped) and is the one vendored file the drift test can check automatically.

---

## Dev mode (hot-reload)

Runs the FastAPI backend with `--reload` and the Vite dev server simultaneously:

```bash
make dashboard-dev
```

Backend: http://localhost:8001 · Frontend dev server: http://localhost:5173

---

## Project layout

```
condor-bench/
├── bench/              # Core benchmark logic
│   ├── client.py       #   LLM client (pydantic-ai + ACP routing, multi-turn)
│   ├── dataset.py      #   Case types and loaders
│   ├── scorer.py       #   Composite scoring
│   ├── baseline.py     #   Baseline latency store
│   └── reporter.py     #   Save/load run results
├── metrics/            # Individual metrics
│   ├── answer_quality.py  # Reference-free GEval judge
│   ├── tool_accuracy.py   # F1 on tool call sets
│   └── latency.py         # Baseline-relative latency score
├── mock_mcp/           # Mock MCP servers (mirror production tool stack)
│   ├── hummingbot_server.py   # mcp-hummingbot tools
│   └── condor_server.py       # condor memory/journal/skills tools
├── condor_compat/      # Vendored clients from the condor repo
│   ├── acp/            #   pydantic-ai client, ACP client, JSON-RPC peer
│   └── agents/         #   Tick prompt builder + condor/AGENT.md (system prompt, body only)
├── datasets/           # JSONL benchmark cases + tool_surface.json (production surface pin)
├── scripts/            # snapshot_tool_surface.py — re-capture the production tool surface
├── dashboard/
│   ├── backend/app.py  # FastAPI: providers, SSE run streaming, results API
│   └── frontend/       # React + Vite + Recharts
├── baseline/           # Stored baseline latency records (tracked in git — shared reference)
├── results/            # Benchmark run outputs (git-ignored)
├── config.py           # Path constants and score weights
├── runner.py           # CLI entry point (typer)
└── .env                # API keys (never commit)
```
