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


def test_dataset_expected_tools_exist_in_production():
    """Every expected_tools entry must name a tool production actually has."""
    snapshot = _snapshot()
    known = {
        tool
        for server in snapshot["servers"].values()
        for tool in server["tools"]
    }

    unknown: dict[str, list[str]] = {}
    for name in ("consult.jsonl", "tick.jsonl"):
        path = ROOT / "datasets" / name
        if not path.exists():
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            for tool in json.loads(line).get("expected_tools") or []:
                if tool not in known:
                    unknown.setdefault(f"{name}:{lineno}", []).append(tool)

    assert not unknown, (
        f"datasets reference tools production does not expose: {unknown}. "
        "Scoring against these can never be satisfied by a correct model."
    )
