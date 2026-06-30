"""Benchmark configuration and path constants."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).parent

DATASETS_DIR = ROOT / "datasets"
BASELINE_DIR = ROOT / "baseline"
RESULTS_DIR = ROOT / "results"

# Mock MCP server script — path relative to this file, works in both
# editable installs (pip install -e .) and regular installs.
MOCK_MCP_SCRIPT = ROOT / "mock_mcp" / "server.py"

BASELINE_MODEL = os.environ.get("BENCH_BASELINE_MODEL", "anthropic:claude-sonnet-4-6")
JUDGE_MODEL = os.environ.get("BENCH_JUDGE_MODEL", "claude-sonnet-4-6")

# Composite score weights
SCORE_WEIGHTS = {
    "answer_quality": 0.50,
    "tool_accuracy": 0.30,
    "latency_score": 0.20,
}

# Latency floor: even the slowest model gets at least this score
LATENCY_FLOOR = 0.1
