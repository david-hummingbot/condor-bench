.PHONY: install baseline test report dashboard dashboard-dev clean

# ── Setup ──────────────────────────────────────────────────────────────────────

install:
	uv sync

# ── Workflow ───────────────────────────────────────────────────────────────────

# Step 1: generate baseline responses using Claude Sonnet (requires ANTHROPIC_API_KEY)
baseline:
	uv run python runner.py baseline

# Step 2: benchmark a specific model against the baseline
# Usage: make test MODEL=ollama:llama3.1:8b
test:
	uv run python runner.py test $(MODEL)

# Step 3: print a summary table of all runs
report:
	uv run python runner.py report

# Step 4: build the React frontend, then serve everything on http://localhost:8001
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
