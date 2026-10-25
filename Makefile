.PHONY: install test-suite baseline test report dashboard dashboard-dev clean \
        tool-surface check-drift case-prompts staging-check market-check register-server \
        clean-journals \
        sweep matrix route test-pr test-nightly test-release promote

# The dashboard holds API keys and can place orders, so it listens on loopback only.
# For deliberate LAN access: make dashboard DASHBOARD_HOST=0.0.0.0 and set
# DASHBOARD_ALLOWED_HOSTS to the name or address you will browse to.
DASHBOARD_HOST ?= 127.0.0.1

# ── Setup ──────────────────────────────────────────────────────────────────────

install:
	uv sync

# Full unit test suite (fast — no model calls).
test-suite:
	uv run python -m pytest -q

# ── Keeping bench honest against condor ────────────────────────────────────────

# Re-capture the production MCP tool surface from the real condor servers.
# Run after pulling condor; commit the resulting datasets/tool_surface.json.
# Usage: make tool-surface [CONDOR_REPO=/path/to/condor]
tool-surface:
	uv run python scripts/snapshot_tool_surface.py

# Fail if the MCP wiring, the vendored system prompt, or condor's agent roster
# have drifted from condor. Run this after every condor pull.
#
# The roster check is here because it is condor drift, not a routing bug: three
# agents (xrpl_market_maker, smart_money_flow, meteora_launch_lp) shipped
# upstream unnoticed because nothing failed when bench had no domain for them.
check-drift:
	uv run python -m pytest tests/test_mcp_wiring_drift.py \
	              tests/test_vendored_drift.py \
	              tests/test_acp_upstream_drift.py \
	              tests/test_matrix_routing.py::test_config_keys_name_agents_condor_actually_ships \
	              tests/test_matrix_routing.py::test_every_shipped_agent_has_a_routing_domain -q

# Regenerate the dashboard's case_id → question and case_id → layer maps after
# editing a dataset. tests/test_dashboard_case_maps.py fails until you do.
case-prompts:
	uv run python scripts/sync_case_prompts.py

# ── Staging ────────────────────────────────────────────────────────────────────

# Fail-closed pre-flight. Exits non-zero on a blocking failure, so it works as a
# gate before any run. See docs/STAGING.md.
staging-check:
	uv run python runner.py staging-check

# Which cases the target's connectors can support. Read-only; exits non-zero
# when a case names a connector this box does not have keys for.
market-check:
	uv run python runner.py market-check

# Register BENCH_SERVER_NAME in condor's config.yml from bench's env vars.
register-server:
	uv run python scripts/register_bench_server.py

# Clear the journals of bench's own probe agents (bench_* slugs only).
# There is no MCP journal delete, so per-case teardown cannot reverse a journal
# write — entries accumulate across sweeps until journal_read responses crowd the
# judge's context. Dry-run by default; APPLY=1 to delete.
clean-journals:
	uv run python scripts/clean_probe_journals.py $(if $(APPLY),--apply,)

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
matrix:
	uv run python runner.py matrix

# Step 5: recommend the smallest passing model per domain
# Usage: make route [MIN_PASS_RATE=0.85] [PREFER_LOWER_TOKENS=1]
route:
	uv run python runner.py route \
		$(if $(MIN_PASS_RATE),--min-pass-rate $(MIN_PASS_RATE),) \
		$(if $(PREFER_LOWER_TOKENS),--prefer-lower-tokens,)

# Print a summary table of all runs
report:
	uv run python runner.py report

# ── Gates (CI later — see docs/decision_log_template.md and the README) ───────
# Which host runs these on a schedule is still open; until then they are the
# same commands a person runs. Model bands are pending too — override the lists.

PR_MODEL ?= openrouter:openai/gpt-4o-mini
NIGHTLY_MODELS ?= openrouter:openai/gpt-4o-mini nvidia:openai/gpt-oss-20b
RELEASE_MODEL ?= $(PR_MODEL)

# PR: unit tests, L0 tool contracts and the regression split — all on the mock
# lane, so a PR never touches staging. Regression cases the mock cannot serve come
# back as named harness artifacts, not failures.
test-pr:
	uv run python -m pytest -q
	uv run python runner.py ladder --upto l0
	BENCH_ENV_MODE=mock uv run python runner.py test $(PR_MODEL) --split regression

# Nightly: per priority model, L1 on the mock, then L2 — the regression split on
# the live lane — with 3 trials so pass rates carry usable intervals.
test-nightly:
	@for m in $(NIGHTLY_MODELS); do \
		uv run python runner.py ladder $$m --upto l1 || exit 1; \
		uv run python runner.py test $$m --split regression --trials 3 || exit 1; \
	done

# Release: full capability with holdout on live (3 trials), then the safety
# category on the mock, where the faults live staging cannot stage are armed.
test-release:
	uv run python runner.py staging-check
	uv run python runner.py sweep --release --trials 3
	BENCH_ENV_MODE=mock uv run python runner.py test $(RELEASE_MODEL) \
		--task-category negative_safety --release --trials 3

# Contract promotion (Experiment A on holdout). Usage:
#   make promote BASELINE=<run_dir> CANDIDATE=<run_dir> LADDER=<ladder id>
promote:
	uv run python runner.py promote --baseline $(BASELINE) --candidate $(CANDIDATE) \
		$(if $(LADDER),--ladder $(LADDER),)

# ── Dashboard ──────────────────────────────────────────────────────────────────

# Build the React frontend, then serve everything on http://localhost:8001
dashboard:
	@bash -c 'fuser -k 8001/tcp 2>/dev/null; sleep 0.5; exit 0'
	cd dashboard/frontend && npm install --silent && npm run build
	@echo ""
	@echo "  Dashboard → http://localhost:8001"
	@echo ""
	uv run uvicorn dashboard.backend.app:app --host $(DASHBOARD_HOST) --port 8001

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

# Re-vendor condor's prompt builders into condor_compat/ (see README).
# Usage: make revendor [CONDOR_PATH=/path/to/condor]
revendor:
	uv run python scripts/revendor.py $(if $(CONDOR_PATH),--condor $(CONDOR_PATH),)

revendor-check:
	uv run python scripts/revendor.py --check $(if $(CONDOR_PATH),--condor $(CONDOR_PATH),)
