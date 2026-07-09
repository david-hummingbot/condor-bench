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
| **Answer quality** | 50% | Reference-free judge (Claude evaluates the response against Condor domain criteria — no baseline needed) |
| **Tool accuracy** | 30% | F1 score comparing actual tool calls against the expected tools defined in the dataset |
| **Latency** | 20% | `baseline_latency / test_latency`, capped at 1.0 — only meaningful after running a baseline |

**Composite** = 0.5 × quality + 0.3 × tools + 0.2 × latency

A case **passes** when composite ≥ 0.70.

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
uv run python runner.py test anthropic:claude-sonnet-4-6 --consult-only

# Tick cases only, filter by category
uv run python runner.py test ollama:qwen2.5:14b --tick-only -c risk

# Print a summary table of all runs
make report
```

Results are saved to `results/<run-id>_<model>/` as JSON.

---

## Dataset

| File | Cases | Description |
|------|-------|-------------|
| `datasets/consult.jsonl` | 23 | Q&A cases covering concepts, strategy, risk, troubleshooting, configuration, strategy creation, and routine building. Includes 5 multi-turn cases. |
| `datasets/tick.jsonl` | 6 | Strategy tick cases: normal execution, profit-taking, risk-blocked, near-capacity, error recovery, dry-run observation. |

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
│   ├── agents/         #   Tick prompt builder
│   └── assistants/     #   AGENT.md (Condor system prompt, body only)
├── datasets/           # JSONL benchmark cases
├── dashboard/
│   ├── backend/app.py  # FastAPI: providers, SSE run streaming, results API
│   └── frontend/       # React + Vite + Recharts
├── baseline/           # Stored baseline latency records (git-ignored)
├── results/            # Benchmark run outputs (git-ignored)
├── config.py           # Path constants and score weights
├── runner.py           # CLI entry point (typer)
└── .env                # API keys (never commit)
```
