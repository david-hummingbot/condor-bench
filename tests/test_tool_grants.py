"""Specialist cases must be scored under the tool grant condor gives the agent.

condor scopes an agent to its declared tools (``allowed_tools=agent.tools or None``
in ``runtime/sessions.py``, ``agents/consult.py``, ``agents/engine.py``). Bench used
to offer all 24 regardless, which measured a harder task than production runs — and
because the model-size cap trims ``tool_defs[:limit]`` over whatever was discovered,
a small model on a ``market_making_expert`` case could be handed six tools that
didn't include ``manage_executors`` and fail for a tool it was never shown.

These tests cover the grant loader, the four scope outcomes, and the harness-artifact
guard that catches the failure mode directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bench.client import BenchmarkResult, TurnResult, agent_tool_scope, load_agent_tools
from bench.scorer import _detect_harness_artifact


# ── grant loader ───────────────────────────────────────────────────────────────
def _write_agent(root: Path, slug: str, frontmatter: str) -> None:
    d = root / "agents" / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "AGENT.md").write_text(f"---\n{frontmatter}---\n\nBody text.\n")
    # condor_path() requires this to look like a checkout
    (root / "mcp_servers").mkdir(exist_ok=True)


def test_declared_tools_are_loaded(tmp_path, monkeypatch):
    _write_agent(
        tmp_path,
        "mm",
        "name: MM\ntools:\n  - get_market_data\n  - manage_executors\n",
    )
    monkeypatch.setenv("CONDOR_PATH", str(tmp_path))
    assert load_agent_tools("mm") == ["get_market_data", "manage_executors"]
    assert agent_tool_scope("mm", load_agent_tools("mm")) == "granted"


def test_absent_tools_key_means_full_surface(tmp_path, monkeypatch):
    """condor, directional_trader and smart_money_flow are defined this way.

    No allowlist is correct — but it must not be confused with a grant of zero
    tools, which would offer the model nothing at all.
    """
    _write_agent(tmp_path, "generalist", "name: Generalist\n")
    monkeypatch.setenv("CONDOR_PATH", str(tmp_path))
    assert load_agent_tools("generalist") is None
    assert agent_tool_scope("generalist", None) == "full_surface"


def test_empty_tools_list_is_not_a_zero_tool_grant(tmp_path, monkeypatch):
    """`tools:` with nothing under it must not blank the model's tool set."""
    _write_agent(tmp_path, "empty", "name: Empty\ntools:\n")
    monkeypatch.setenv("CONDOR_PATH", str(tmp_path))
    assert load_agent_tools("empty") is None


def test_chat_scoped_and_synthetic_slugs_are_distinguished(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDOR_PATH", str(tmp_path))
    (tmp_path / "mcp_servers").mkdir(exist_ok=True)
    assert agent_tool_scope(None, None) == "chat_scoped"
    # A tick slug like bench_tick_normal has no AGENT.md upstream.
    assert agent_tool_scope("bench_tick_normal", None) == "no_agent_md"


def test_broken_frontmatter_does_not_raise(tmp_path, monkeypatch):
    d = tmp_path / "agents" / "broken"
    d.mkdir(parents=True)
    (d / "AGENT.md").write_text("---\ntools: [unclosed\n---\nbody\n")
    (tmp_path / "mcp_servers").mkdir(exist_ok=True)
    monkeypatch.setenv("CONDOR_PATH", str(tmp_path))
    assert load_agent_tools("broken") is None


def test_no_condor_checkout_yields_no_grant(monkeypatch, tmp_path):
    monkeypatch.setenv("CONDOR_PATH", str(tmp_path / "nope"))
    assert load_agent_tools("market_making_expert") is None


# ── harness-artifact guard ─────────────────────────────────────────────────────
def _result(wiring: dict) -> BenchmarkResult:
    return BenchmarkResult(
        case_id="x", model="m", turns=[TurnResult("hi", [], 1.0)], wiring=wiring
    )


_BASE = {"api_url": "http://staging:8000", "autodiscovery_extras": []}


def test_expected_tool_never_offered_is_an_artifact():
    """The exact failure scoping exists to prevent: cut by the model-size cap."""
    reason = _detect_harness_artifact(
        _result({**_BASE, "offered_tools": ["get_market_data", "manage_bots"]}),
        ["manage_executors"],
    )
    assert reason and "manage_executors" in reason
    assert "never offered" in reason


def test_offered_tools_covering_expectations_is_clean():
    assert (
        _detect_harness_artifact(
            _result({**_BASE, "offered_tools": ["get_market_data", "manage_executors"]}),
            ["manage_executors"],
        )
        is None
    )


def test_namespaced_offered_names_still_match():
    """MCP namespaces tool names; the guard must not fire on the prefix alone."""
    assert (
        _detect_harness_artifact(
            _result({**_BASE, "offered_tools": ["mcp__condor__manage_skill"]}),
            ["manage_skill"],
        )
        is None
    )


def test_unrecorded_offered_tools_does_not_fire():
    """ACP runs report nothing; absence of data is not evidence of a bad row."""
    assert _detect_harness_artifact(_result(dict(_BASE)), ["manage_executors"]) is None


def test_advisory_case_with_no_expected_tools_does_not_fire():
    assert (
        _detect_harness_artifact(_result({**_BASE, "offered_tools": ["x"]}), []) is None
    )


# ── the real condor roster ─────────────────────────────────────────────────────
def test_scoped_specialists_get_a_smaller_grant_than_the_full_surface():
    """Integration check against the resolved checkout. Skips without one."""
    from config import condor_path

    repo = condor_path()
    if repo is None or not (repo / "agents").is_dir():
        pytest.skip("no condor checkout — set CONDOR_PATH to enable this check")

    grant = load_agent_tools("market_making_expert")
    if grant is None:
        pytest.skip("market_making_expert declares no tools upstream")
    assert 0 < len(grant) < 24, f"expected a scoped grant, got {len(grant)}"
    assert "get_market_data" in grant
