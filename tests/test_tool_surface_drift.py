"""The mocks must expose the same tool surface as the real condor MCP servers.

Benchmarks run against mock_mcp/, not production. If a real server adds, drops,
or renames a tool and the mocks don't follow, tool-accuracy scores are measured
against a surface production doesn't have — and nothing else in the suite
notices. These tests compare the mocks (and the dataset's expected_tools)
against datasets/tool_surface.json, refreshed by `make tool-surface`.

A failure here means one of two things: the mocks are stale and need updating,
or production legitimately changed and the snapshot needs regenerating. It never
means "edit the snapshot by hand".
"""
from __future__ import annotations

import ast
import json
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = ROOT / "datasets" / "tool_surface.json"

# snapshot server label -> mock module implementing it
MOCK_FOR_SERVER = {
    "mcp-hummingbot": ROOT / "mock_mcp" / "hummingbot_server.py",
    "condor": ROOT / "mock_mcp" / "condor_server.py",
}

REFRESH_HINT = "Run `make tool-surface` if production changed; otherwise update the mock."


def test_snapshot_commit_is_reachable_from_the_resolved_checkout():
    """The pinned surface must come from the checkout everything else reads.

    With more than one condor clone on a machine — normal, since one is usually a
    feature branch with work in progress — ``condor_path()`` can resolve a
    different one than the snapshot came from. Every drift check then compares
    bench against a condor nobody is running, which looks exactly like real drift;
    the natural response (re-vendor, regenerate) would sync bench to the wrong
    tree. So it is named explicitly.

    **Reachability, not object existence.** An earlier version asked whether the
    commit was in the object database, which a plain ``git fetch`` in a
    feature-branch clone would satisfy while that clone's *working tree* — the
    thing the drift checks actually read — still held different code. Ancestry from
    HEAD is the question that matches what gets loaded.

    A snapshot legitimately lags HEAD between syncs, so being *behind* is fine;
    what fails is the snapshot's commit not being in this checkout's history at
    all.
    """
    import subprocess

    from config import condor_checkout_label, condor_checkout_state, condor_path

    repo = condor_path()
    if repo is None:
        pytest.skip("no condor checkout — set CONDOR_PATH to enable this check")

    recorded = _snapshot().get("source_commit", "").split()[0]
    if not recorded or recorded == "unknown":
        pytest.skip("snapshot records no source commit")

    try:
        reachable = (
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", recorded, "HEAD"],
                cwd=repo,
                capture_output=True,
                timeout=5,
            ).returncode
            == 0
        )
    except Exception:
        pytest.skip("git unavailable")

    state = condor_checkout_state()
    assert reachable, (
        f"datasets/tool_surface.json was captured at condor commit {recorded}, which "
        f"is not in the history of the checkout bench resolves:\n"
        f"  {condor_checkout_label()}\n"
        f"That checkout is on '{state['branch']}'. Either CONDOR_PATH points at a "
        "different clone or branch than the snapshot came from, or that branch "
        "predates the snapshot. Fix this before treating any other drift failure as "
        "real — re-vendoring against the wrong tree is worse than the drift."
    )


def test_resolved_condor_checkout_is_clean():
    """Uncommitted condor edits make the drift checks unreproducible.

    The checks import condor's ``_shared.py`` and read its ``agents/`` tree from
    the working directory, so local edits are what gets measured — a result no one
    else can reproduce and that corresponds to no commit. Advisory (a warning, not
    a failure): editing condor while iterating on bench is a legitimate workflow,
    it just shouldn't be invisible.
    """
    from config import condor_checkout_label, condor_checkout_state

    state = condor_checkout_state()
    if state.get("path") is None:
        pytest.skip("no condor checkout — set CONDOR_PATH to enable this check")

    if state["dirty_files"]:
        warnings.warn(
            f"condor checkout has {state['dirty_files']} uncommitted file(s); drift "
            f"results reflect local edits, not any commit: {condor_checkout_label()}",
            stacklevel=1,
        )


def _snapshot() -> dict:
    if not SNAPSHOT_PATH.exists():
        pytest.skip(f"{SNAPSHOT_PATH.name} missing — run `make tool-surface`")
    return json.loads(SNAPSHOT_PATH.read_text())


def _mock_tools(path: Path) -> dict[str, set[str]]:
    """Map tool name -> parameter names for every @mcp.tool() in a mock module."""
    tree = ast.parse(path.read_text())
    tools: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorated = any(
            isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "tool"
            for d in node.decorator_list
        )
        if decorated:
            tools[node.name] = {
                a.arg for a in list(node.args.args) + list(node.args.kwonlyargs)
            }
    return tools


SERVERS = sorted(MOCK_FOR_SERVER)


@pytest.mark.parametrize("server", SERVERS)
def test_mock_exposes_every_production_tool(server):
    production = set(_snapshot()["servers"][server]["tools"])
    mocked = set(_mock_tools(MOCK_FOR_SERVER[server]))

    missing = production - mocked
    assert not missing, (
        f"{MOCK_FOR_SERVER[server].name} is missing tools that production exposes: "
        f"{sorted(missing)}. {REFRESH_HINT}"
    )


@pytest.mark.parametrize("server", SERVERS)
def test_mock_exposes_no_phantom_tools(server):
    """A mock-only tool lets a model 'succeed' on a call production would reject."""
    production = set(_snapshot()["servers"][server]["tools"])
    mocked = set(_mock_tools(MOCK_FOR_SERVER[server]))

    phantom = mocked - production
    assert not phantom, (
        f"{MOCK_FOR_SERVER[server].name} exposes tools production does not have: "
        f"{sorted(phantom)}. {REFRESH_HINT}"
    )


@pytest.mark.parametrize("server", SERVERS)
def test_mock_accepts_every_required_production_param(server):
    """Params production marks required must be nameable on the mock.

    FastMCP tolerates unexpected kwargs, so a mock lacking one of these does not
    error — it silently ignores the argument and returns the same canned payload
    for every value. That hides real behavioural differences (e.g. a
    `data_type` that selects between candles and trades) behind a passing call.
    """
    tools = _snapshot()["servers"][server]["tools"]
    mocked = _mock_tools(MOCK_FOR_SERVER[server])

    gaps = {
        name: sorted(set(spec["required"]) - mocked[name])
        for name, spec in tools.items()
        if name in mocked and set(spec["required"]) - mocked[name]
    }
    assert not gaps, (
        f"{MOCK_FOR_SERVER[server].name} ignores required production params: "
        f"{gaps}. {REFRESH_HINT}"
    )


DATASET_FILES = ("consult.jsonl", "tick.jsonl", "tools.jsonl", "agents.jsonl")


def _dataset_records() -> list[tuple[str, dict]]:
    """Every dataset record, tagged with a "file:lineno" label for error messages."""
    records = []
    for name in DATASET_FILES:
        path = ROOT / "datasets" / name
        if not path.exists():
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            line = line.strip()
            if line:
                records.append((f"{name}:{lineno}", json.loads(line)))
    return records


def _known_tools() -> dict[str, dict]:
    """tool name -> production spec, across both servers."""
    return {
        tool: spec
        for server in _snapshot()["servers"].values()
        for tool, spec in server["tools"].items()
    }


def test_dataset_expected_tools_exist_in_production():
    """Every expected_tools entry must name a tool production actually has."""
    known = _known_tools()

    unknown: dict[str, list[str]] = {}
    for label, data in _dataset_records():
        expected = list(data.get("expected_tools") or [])
        expected += list(data.get("expected_tool_calls") or [])
        expected += list(data.get("expected_no_calls") or [])
        # A tool case names its subject directly, so that field is ground truth too.
        if data.get("tool"):
            expected.append(data["tool"])
        for tool in expected:
            if tool not in known:
                unknown.setdefault(label, []).append(tool)

    assert not unknown, (
        f"datasets reference tools production does not expose: {unknown}. "
        "Scoring against these can never be satisfied by a correct model."
    )


def test_dataset_expected_params_exist_in_production():
    """Pinned params must be real parameters of the tool they're pinned on.

    A typo here is invisible without this check: ``metrics/tool_params`` looks the
    key up in the call's arguments, never finds it, and scores every model 0 on a
    case no model can pass.
    """
    known = _known_tools()

    bad: dict[str, list[str]] = {}
    for label, data in _dataset_records():
        for tool, params in (data.get("expected_tool_params") or {}).items():
            spec = known.get(tool)
            if spec is None:
                bad.setdefault(label, []).append(f"{tool} (unknown tool)")
                continue
            for key in params:
                if key not in spec["params"]:
                    bad.setdefault(label, []).append(f"{tool}.{key}")

    assert not bad, (
        f"datasets pin parameters production's tools do not accept: {bad}. "
        f"{REFRESH_HINT}"
    )


def test_every_production_tool_has_a_tool_case():
    """datasets/tools.jsonl must cover the whole MCP surface.

    Layer 2 exists to answer "which model size can call *this* tool correctly".
    An uncovered tool is a hole in the routing matrix that reads as "no data"
    rather than as a gap someone chose.
    """
    path = ROOT / "datasets" / "tools.jsonl"
    if not path.exists():
        pytest.skip("datasets/tools.jsonl missing")

    covered = {
        json.loads(line)["tool"]
        for line in path.read_text().splitlines()
        if line.strip()
    }
    missing = set(_known_tools()) - covered
    assert not missing, (
        f"no per-tool benchmark case for: {sorted(missing)}. Add one to "
        "datasets/tools.jsonl, or the matrix silently has no verdict for it."
    )


def test_agent_scoped_cases_declare_a_slug():
    """Tick and agent cases must resolve to an agent_slug (or explicit null).

    Layer 3 and tick cases act *as* an agent. A missing slug sends condor's
    memory/skill/journal tools at the chat's stores instead, and the case fails
    for a harness reason that looks exactly like a model limitation.
    """
    from bench.dataset import load_agent_cases, load_tick_cases

    missing = [c.id for c in load_tick_cases() if not c.agent_slug]
    assert not missing, f"tick cases without an agent_slug: {missing}"

    # For agent cases an explicit null is legitimate (chat-scoped, like a
    # production consult), so check the raw JSON has the key rather than a value.
    path = ROOT / "datasets" / "agents.jsonl"
    if path.exists():
        undeclared = [
            json.loads(line)["id"]
            for line in path.read_text().splitlines()
            if line.strip() and "agent_slug" not in json.loads(line)
        ]
        assert not undeclared, (
            f"agent cases that don't declare agent_slug (use null for chat-scoped): "
            f"{undeclared}"
        )
    assert load_agent_cases() is not None
