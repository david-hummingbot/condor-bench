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

BASELINE_MODEL = os.environ.get("BENCH_BASELINE_MODEL", "anthropic:claude-sonnet-5")
JUDGE_MODEL = os.environ.get("BENCH_JUDGE_MODEL", "claude-sonnet-5")


# ── Execution mode ─────────────────────────────────────────────────────────────
# "live" runs against a staging hummingbot-api through condor's real MCP servers;
# "mock" runs the offline mock_mcp/ servers (CI, drift checks, no staging).
def bench_mode() -> str:
    """Resolved execution mode. Read per call so tests/dashboard can override."""
    mode = (os.environ.get("BENCH_MODE") or "mock").strip().lower()
    return mode if mode in ("live", "mock") else "mock"


def condor_path() -> Path | None:
    """Path to the condor checkout that provides the production MCP wiring.

    Falls back to a sibling ../condor checkout, which is how the repos are laid
    out in development. Returns None when neither resolves to a real checkout,
    so live mode can fail with a clear message instead of an ImportError.

    Set CONDOR_PATH explicitly if more than one condor clone exists on the
    machine. The fallback cannot tell them apart, and picking the stale one makes
    every drift check compare against the wrong upstream — which reads as "condor
    changed" rather than "you're pointed at the wrong clone". See
    :func:`condor_checkout_label`.
    """
    raw = os.environ.get("CONDOR_PATH") or os.environ.get("CONDOR_REPO")
    candidate = Path(raw).expanduser() if raw else ROOT.parent / "condor"
    return candidate.resolve() if (candidate / "mcp_servers").is_dir() else None


def condor_head() -> str:
    """Short HEAD commit of the resolved condor checkout, or "unknown"."""
    import subprocess

    repo = condor_path()
    if repo is None:
        return "unknown"
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
    except Exception:
        return "unknown"


def condor_checkout_label() -> str:
    """"path @ commit (source: CONDOR_PATH | default)" for diagnostics.

    Every drift failure quotes this. Without it, a mismatch caused by pointing at
    the wrong clone is indistinguishable from real upstream drift, and the natural
    response — re-vendoring — would sync bench to the wrong condor.
    """
    repo = condor_path()
    if repo is None:
        return "no condor checkout (set CONDOR_PATH)"
    source = (
        "CONDOR_PATH"
        if os.environ.get("CONDOR_PATH")
        else ("CONDOR_REPO" if os.environ.get("CONDOR_REPO") else "default ../condor")
    )
    return f"{repo} @ {condor_head()} (via {source})"


# ── Staging environment (live mode) ────────────────────────────────────────────
def staging_config() -> dict[str, object]:
    """Staging identifiers for live runs, read fresh from the environment."""
    return {
        "api_url": (os.environ.get("HUMMINGBOT_API_URL") or "").rstrip("/"),
        # Alias used by the fail-closed check. Defaults to HUMMINGBOT_API_URL so a
        # single-var setup still gets the guard; setting both to different values
        # is a configuration error the health check reports.
        "expected_api_url": (
            os.environ.get("BENCH_EXPECTED_API_URL")
            or os.environ.get("HUMMINGBOT_API_URL")
            or ""
        ).rstrip("/"),
        "username": os.environ.get("HUMMINGBOT_USERNAME", ""),
        "password": os.environ.get("HUMMINGBOT_PASSWORD", ""),
        "server_name": os.environ.get("BENCH_SERVER_NAME", "bench_staging"),
        "chat_id": int(os.environ.get("BENCH_CHAT_ID", "999001")),
        "user_id": int(os.environ.get("BENCH_USER_ID", "999001")),
        "account": os.environ.get("BENCH_STAGING_ACCOUNT", "bench_paper"),
        "allow_mutating": _env_flag("BENCH_ALLOW_MUTATING", False),
    }


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ── Composite score weights ────────────────────────────────────────────────────
# Mock mode: the historical weights. Tool params and live validity can't be
# judged against canned responses, so they carry no weight here.
SCORE_WEIGHTS_MOCK = {
    "answer_quality": 0.50,
    "tool_accuracy": 0.30,
    "tool_params": 0.00,
    "live_validity": 0.00,
    "latency_score": 0.20,
}

# Live mode: real API responses make param correctness and response shape
# meaningful, so tool-name F1 gives up weight to them.
SCORE_WEIGHTS_LIVE = {
    "answer_quality": 0.45,
    "tool_accuracy": 0.20,
    "tool_params": 0.15,
    "live_validity": 0.10,
    "latency_score": 0.10,
}

# Backwards-compatible alias: existing callers (and tests) that import
# SCORE_WEIGHTS get the mock profile, which is what they were written against.
SCORE_WEIGHTS = SCORE_WEIGHTS_MOCK


def score_weights(mode: str | None = None) -> dict[str, float]:
    """Weight profile for a mode. Unknown modes fall back to mock."""
    return SCORE_WEIGHTS_LIVE if (mode or bench_mode()) == "live" else SCORE_WEIGHTS_MOCK


# Latency floor: even the slowest model gets at least this score
LATENCY_FLOOR = 0.1

# A case passes when its composite reaches this. Also the per-case bar the
# matrix uses to compute domain pass rates.
PASS_THRESHOLD = 0.70

# A model "passes a domain" at this pass rate (see bench/routing.py).
DOMAIN_PASS_RATE = 0.80

# Destructive cases get a higher floor: a model that passes a domain on average
# but botches an irreversible action is not a routing candidate.
DESTRUCTIVE_FLOOR = 0.70
