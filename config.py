"""Benchmark configuration and path constants."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).parent

# Load .env here, at the one module every entry point imports before reading an env
# var. Without it CONDOR_PATH is unset for anything that is not runner.py or the
# dashboard, and condor_path() silently falls back to a sibling ../condor — which on
# a machine with two clones is a different, usually older checkout.
#
# This has now caused three separate wrong answers: a maintenance script that would
# have deleted from the wrong checkout, and two analyses that reported an agent as
# having no tool grant because the fallback clone predates it. It also removes an
# accidental dependency — under pytest the only reason .env was loaded at all is that
# importing deepeval calls load_dotenv() as a side effect, so dropping an unrelated
# test dependency would have silently changed which condor every drift check read.
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover — dotenv is a declared dependency
    pass

DATASETS_DIR = ROOT / "datasets"
BASELINE_DIR = ROOT / "baseline"
RESULTS_DIR = ROOT / "results"
SUITES_DIR = ROOT / "suites"
ENVIRONMENTS_DIR = SUITES_DIR / "environments"

BASELINE_MODEL = os.environ.get("BENCH_BASELINE_MODEL", "anthropic:claude-sonnet-5")
JUDGE_MODEL = os.environ.get("BENCH_JUDGE_MODEL", "claude-sonnet-5")


def condor_path() -> Path | None:
    """Path to the condor checkout that provides the production MCP wiring.

    Falls back to a sibling ../condor checkout, which is how the repos are laid
    out in development. Returns None when neither resolves to a real checkout,
    so the wiring can fail with a clear message instead of an ImportError.

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
    """Paths the MCP wiring would / did resolve against the current checkout.

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
    # A cancelled run is saved so its scored cases are not thrown away, but it must
    # not feed the matrix by default. Cell ownership is newest-run-wins, so a run
    # cancelled at case 12 would claim its model's domain and tool cells on 12 cases
    # and shadow a complete 90-case run from the day before — replacing good evidence
    # with less of it. Opt in with `include_in_matrix` when the partial set is what
    # you actually want measured.
    if summary.get("partial") is True:
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


# ── Staging environment ────────────────────────────────────────────────────────
# Fixed internal identity for live runs. Operators only configure the Hummingbot
# API URL + credentials (Settings); server name / chat / user are owned by bench
# so manage_servers ACL and MCP --server-name stay consistent without per-machine
# hand-editing. See bench/staging_setup.ensure_bench_server().
BENCH_SERVER_NAME = "bench_staging"
BENCH_CHAT_ID = 999001
BENCH_USER_ID = 999001


def staging_config() -> dict[str, object]:
    """Staging identifiers for benchmark runs, read fresh from the environment."""
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
        "server_name": os.environ.get("BENCH_SERVER_NAME") or BENCH_SERVER_NAME,
        "chat_id": int(os.environ.get("BENCH_CHAT_ID") or BENCH_CHAT_ID),
        "user_id": int(os.environ.get("BENCH_USER_ID") or BENCH_USER_ID),
    }


# ── Composite score weights ────────────────────────────────────────────────────
# Real API responses make param correctness and response shape meaningful, so
# tool-name F1 shares weight with them rather than carrying the tool signal alone.
SCORE_WEIGHTS = {
    "answer_quality": 0.45,
    "tool_accuracy": 0.20,
    "tool_params": 0.15,
    "live_validity": 0.10,
    "latency_score": 0.10,
}

# Latency floor: even the slowest model gets at least this score
LATENCY_FLOOR = 0.1

# A case passes when its composite reaches this. Also the per-case bar the
# matrix uses to compute domain pass rates.
PASS_THRESHOLD = 0.70

# A model "passes a domain" at this pass rate (see bench/routing.py).
DOMAIN_PASS_RATE = 0.80

# A model "handles a tool" at this pass rate — deliberately lower than the domain
# bar, and not a fudge. The two answer different questions: a domain verdict is
# "can this model own this job", a tool verdict is "can it drive this tool at all".
#
# The number is also forced by arithmetic. At 0.80 the sample sizes a per-tool axis
# can afford (2-4 cases) all require a *perfect* score — 2/3 and 3/4 both fall
# below it — so one unlucky case reads as "no model handles this tool". 0.65 lets
# 2/3 pass, which is the point of asking for three cases instead of two.
#
# It is 0.65 and not 0.67 because 2/3 is 0.6666…, so a 0.67 bar would reject the
# exact outcome this bar exists to allow. Raising it back to 0.80 only makes sense
# alongside MIN_TOOL_CASES >= 5, the first size where one miss still passes.
TOOL_PASS_RATE = 0.65

# Scored cases a model needs before a per-tool verdict counts as evidence. Below
# this the tool is reported as thin rather than as handled or unhandled: a single
# case is a coin flip wearing a verdict's clothes.
MIN_TOOL_CASES = 3

# Destructive cases get a higher floor: a model that passes a domain on average
# but botches an irreversible action is not a routing candidate.
DESTRUCTIVE_FLOOR = 0.70

# A case that declares post_conditions is asserting an end state — the routine
# exists, the memory is stored. If that state never materialised the case did not
# achieve its purpose, whatever it said in prose, so its composite is capped here
# and it cannot pass.
#
# Folding post-conditions into live_validity alone was not enough: that metric
# carries 0.10, so a failed build cost 0.05 of composite against a 0.70 threshold
# and still passed. The cap is what makes the assertion mean something.
#
# Note this has to move the *composite*: bench/matrix.py recomputes pass from
# `composite >= PASS_THRESHOLD` rather than reading ScoreCard.passed, so a flag on
# the scorecard would never reach a domain pass rate.
POST_CONDITION_FAIL_CAP = 0.50

# Wall-clock ceiling for a single case, in seconds. Runs are serial, so one case
# that never returns stalls the whole sweep behind it — `c012` ("what skills do
# you have?", a bare `manage_skill:list`) once took 609s, 23% of a 45-minute
# suite, for a lookup whose median is under 16s.
#
# A timeout is scored as an infra failure, not as a 0: the model was not measured,
# so excluding it is the honest reading (see bench/matrix.py). That means a
# too-tight value silently thins the tool axis rather than failing loudly, which
# is why this is well above the slowest legitimate case rather than near it.
CASE_TIMEOUT_S = 180.0
