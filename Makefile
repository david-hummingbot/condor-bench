.PHONY: install test-suite baseline test report dashboard dashboard-dev clean \
        tool-surface check-drift case-prompts staging-check register-server \
        sweep matrix route

# ── Setup ──────────────────────────────────────────────────────────────────────

install:
	uv sync

# Full unit test suite (fast — no model calls).
test-suite:
	uv run python -m pytest -q

# ── Keeping the mocks honest ───────────────────────────────────────────────────

# Re-capture the production MCP tool surface from the real condor servers.
# Run after pulling condor; commit the resulting datasets/tool_surface.json.
# Usage: make tool-surface [CONDOR_REPO=/path/to/condor]
tool-surface:
	uv run python scripts/snapshot_tool_surface.py

# Fail if the mocks, the live MCP wiring, or the vendored system prompt have
# drifted from condor. Run this after every condor pull.
check-drift:
	uv run python -m pytest tests/test_tool_surface_drift.py tests/test_mcp_wiring_drift.py \
	              tests/test_vendored_drift.py -q

# Regenerate the dashboard's case_id → question map after editing a dataset.
case-prompts:
	uv run python scripts/sync_case_prompts.py

# ── Live mode ──────────────────────────────────────────────────────────────────

# Fail-closed pre-flight. Exits non-zero on a blocking failure, so it works as a
# gate before any live run. See docs/STAGING.md.
staging-check:
	uv run python runner.py staging-check

# Register BENCH_SERVER_NAME in condor's config.yml from bench's env vars.
register-server:
	uv run python scripts/register_bench_server.py

# ── Workflow ───────────────────────────────────────────────────────────────────

# Step 1: generate baseline responses using Claude Sonnet (requires ANTHROPIC_API_KEY)
baseline:
	uv run python runner.py baseline

# Step 2: benchmark a specific model against the baseline
# Usage: make test MODEL=ollama:llama3.1:8b
test:
	uv run python runner.py test $(MODEL)

# Step 3: benchmark every model in the registry, smallest first
# Usage: make sweep [DOMAIN=market_making_expert] [MAX_PARAMS_B=14]
sweep:
	uv run python runner.py sweep \
		$(if $(DOMAIN),-d $(DOMAIN),) \
		$(if $(MAX_PARAMS_B),--max-params-b $(MAX_PARAMS_B),)

# Step 4: aggregate saved runs into a model × domain/tool matrix
# Usage: make matrix [MODE=live]
matrix:
	uv run python runner.py matrix $(if $(MODE),--mode $(MODE),)

# Step 5: recommend the smallest passing model per domain
# Usage: make route [MIN_PASS_RATE=0.85] [PREFER_LOWER_TOKENS=1]
route:
	uv run python runner.py route \
		$(if $(MIN_PASS_RATE),--min-pass-rate $(MIN_PASS_RATE),) \
		$(if $(PREFER_LOWER_TOKENS),--prefer-lower-tokens,)

# Print a summary table of all runs
report:
	uv run python runner.py report

# ── Dashboard ──────────────────────────────────────────────────────────────────

# Build the React frontend, then serve everything on http://localhost:8001
dashboard:
	@bash -c 'fuser -k 8001/tcp 2>/dev/null; sleep 0.5; exit 0'
	cd dashboard/frontend && npm install --silent && npm run build
	@echo ""
	@echo "  Dashboard → http://localhost:8001"
	@echo ""
	uv run uvicorn dashboard.backend.app:app --host 0.0.0.0 --port 8001

# Hot-reload dev mode: backend on :8001, Vite on :5173
dashboard-dev:
	@bash -c 'fuser -k 8001/tcp 2>/dev/null; sleep 0.5; exit 0'
	@trap 'kill 0' SIGINT; \
	uv run uvicorn dashboard.backend.app:app --port 8001 --reload & \
	cd dashboard/frontend && npm install --silent && npm run dev; \
	wait

# ── Shortcuts ──────────────────────────────────────────────────────────────────

# Benchmark common cloud models (all require API keys)
bench-cloud:
	uv run python runner.py test anthropic:claude-haiku-4-5-20251001
	uv run python runner.py test openrouter:openai/gpt-4o-mini
	uv run python runner.py test openrouter:google/gemini-flash-1.5

# Benchmark local models (requires Ollama running)
bench-local:
	uv run python runner.py test ollama:llama3.1:8b
	uv run python runner.py test ollama:qwen2.5:14b
	uv run python runner.py test ollama:mistral:7b

clean:
	rm -rf results/* baseline/*
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
