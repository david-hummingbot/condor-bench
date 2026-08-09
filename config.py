"""Benchmark configuration and path constants."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).parent

DATASETS_DIR = ROOT / "datasets"
BASELINE_DIR = ROOT / "baseline"
RESULTS_DIR = ROOT / "results"
SUITES_DIR = ROOT / "suites"
ENVIRONMENTS_DIR = SUITES_DIR / "environments"

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
    machine. The fallback cannot tell them apart, and it is normal for one clone
    to be a feature branch with work in progress — benchmarking against that
    measures a condor nobody is running, and every drift check then reads as
    "condor changed" rather than "you're pointed at the wrong checkout". See
    :func:`condor_checkout_label`.
    """
    raw = os.environ.get("CONDOR_PATH") or os.environ.get("CONDOR_REPO")
    candidate = Path(raw).expanduser() if raw else ROOT.parent / "condor"
    return candidate.resolve() if (candidate / "mcp_servers").is_dir() else None


def _git(repo: Path, *args: str) -> str | None:
    """Run a git command in ``repo``, or None if it fails."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, timeout=5
        )
    except Exception:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def condor_head() -> str:
    """Short HEAD commit of the resolved condor checkout, or "unknown"."""
    repo = condor_path()
    if repo is None:
        return "unknown"
    return _git(repo, "rev-parse", "--short", "HEAD") or "unknown"


def condor_checkout_state() -> dict[str, object]:
    """Path, branch, commit, dirtiness and how the path was resolved.

    Branch and dirtiness matter as much as the commit: the drift checks read
    condor's *working tree*, so a feature branch — or uncommitted edits — makes
    them measure a condor that exists on no one else's machine.

    ``path`` is always a string (or None) so the dict can be written straight into
    ``summary.json`` without ``json.dumps`` raising on ``Path``.
    """
    repo = condor_path()
    if repo is None:
        return {"path": None}
    dirty = _git(repo, "status", "--porcelain")
    return {
        "path": str(repo),
        "branch": _git(repo, "rev-parse", "--abbrev-ref", "HEAD") or "unknown",
        "commit": _git(repo, "rev-parse", "--short", "HEAD") or "unknown",
        "dirty_files": len([ln for ln in (dirty or "").splitlines() if ln.strip()]),
        "source": (
            "CONDOR_PATH"
            if os.environ.get("CONDOR_PATH")
            else (
                "CONDOR_REPO"
                if os.environ.get("CONDOR_REPO")
                else "default ../condor"
            )
        ),
    }


def condor_loaded_paths(*, shared_loaded: bool = False) -> dict[str, str | None]:
    """Paths the live wiring would / did resolve against the current checkout.

    When ``shared_loaded`` is True, also report the module file that
    ``load_condor_shared`` actually imported (proves subprocess isolation).
    Callers that have not imported condor yet get the expected paths from disk.
    """
    repo = condor_path()
    if repo is None:
        return {
            "shared_py": None,
            "config_yml": None,
            "acp_working_dir": None,
            "sys_path_head": None,
        }
    shared_py = repo / "handlers" / "agents" / "_shared.py"
    loaded_shared: str | None = str(shared_py) if shared_py.is_file() else None
    if shared_loaded:
        import sys

        mod = sys.modules.get("condor_agents_shared")
        if mod is not None and getattr(mod, "__file__", None):
            loaded_shared = str(Path(mod.__file__).resolve())
    return {
        "shared_py": loaded_shared,
        "config_yml": str(repo / "config.yml"),
        "acp_working_dir": str(repo),
        "sys_path_head": str(repo),
    }


def tool_surface_source_commit() -> str | None:
    """Short commit recorded in datasets/tool_surface.json, if any."""
    try:
        import json

        data = json.loads((DATASETS_DIR / "tool_surface.json").read_text())
        raw = str(data.get("source_commit") or "").split()[0]
        return raw or None
    except Exception:
        return None


def build_run_pin(
    *,
    run_type: str = "adhoc",
    suite_id: str | None = None,
    environment_id: str | None = None,
    run_group_id: str | None = None,
    case_ids: list[str] | None = None,
    models: list[str] | None = None,
    mode: str | None = None,
    risk_ceiling: str | None = None,
    include_in_matrix: bool = False,
    shared_loaded: bool = False,
) -> dict[str, object]:
    """Metadata stamped onto every ``summary.json`` for attribution and Compare.

    All paths are strings. ``loaded`` records what the process resolved so a
    mismatch with git ``path`` is visible rather than inferred.
    """
    state = condor_checkout_state()
    loaded = condor_loaded_paths(shared_loaded=shared_loaded)
    surface_commit = tool_surface_source_commit()
    commit = state.get("commit")
    surface_stale = bool(
        surface_commit
        and commit
        and commit != "unknown"
        and surface_commit != commit
    )
    staging = staging_config()
    pin: dict[str, object] = {
        "run_type": run_type,
        "suite_id": suite_id,
        "environment_id": environment_id,
        "run_group_id": run_group_id,
        "include_in_matrix": include_in_matrix,
        "condor": {
            "path": state.get("path"),
            "branch": state.get("branch"),
            "commit": commit,
            "dirty_files": state.get("dirty_files", 0),
            "source": state.get("source"),
            "loaded": loaded,
            "tool_surface_source_commit": surface_commit,
            "tool_surface_stale": surface_stale,
        },
        "case_ids": list(case_ids or []),
        "models": list(models or []),
        "mode": mode or bench_mode(),
        "risk_ceiling": risk_ceiling,
        "allow_mutating": bool(staging["allow_mutating"]),
    }
    return pin


def summary_counts_for_matrix(summary: dict) -> bool:
    """Whether a persisted run should feed the matrix / router.

    Suite runs and custom-prompt runs are excluded unless they explicitly opt in
    via ``include_in_matrix: true``. Hand-edited suite cases must not silently
    reshape routing recommendations.
    """
    if summary.get("include_in_matrix") is True:
        return True
    run_type = summary.get("run_type")
    if run_type in ("suite", "custom_prompt", "custom", "custom-prompt"):
        return False
    if summary.get("suite_id"):
        return False
    return True


def condor_checkout_label() -> str:
    """One-line description of the checkout every drift failure quotes.

    Without it, a mismatch caused by pointing at another checkout is
    indistinguishable from real upstream drift — and the natural response,
    re-vendoring, would sync bench to the wrong condor.
    """
    state = condor_checkout_state()
    if state.get("path") is None:
        return "no condor checkout (set CONDOR_PATH)"
    dirty = state["dirty_files"]
    suffix = f", {dirty} uncommitted file(s)" if dirty else ""
    return (
        f"{state['path']} on {state['branch']} @ {state['commit']}"
        f"{suffix} (via {state['source']})"
    )


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
