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


# ── dataset shape after the consult/agent merge ────────────────────────────────
def test_agents_dataset_holds_only_agent_scoped_cases():
    """A null-slug case in agents.jsonl implies a routing target that isn't real.

    Those cases run the generic Condor prompt against the chat's stores and pool
    into `general_consult` regardless, so filing them as agent cases only inflated
    one domain while looking like coverage of another. They live in consult.jsonl.
    """
    import json

    from config import DATASETS_DIR

    rows = [
        json.loads(line)
        for line in (DATASETS_DIR / "agents.jsonl").read_text().splitlines()
        if line.strip()
    ]
    unscoped = [r["id"] for r in rows if not r.get("agent_slug")]
    assert not unscoped, (
        f"agents.jsonl has chat-scoped cases: {unscoped}. Move them to "
        "consult.jsonl — agent_slug: null is general_consult work."
    )


def test_every_dataset_slug_is_real_or_a_declared_bench_synthetic():
    """A typo'd slug silently becomes its own domain with no config key.

    `market_making_exprt` would produce a routing domain nothing can apply, and it
    would look like a legitimately unmet domain rather than a dataset bug. Tick
    slugs are bench-owned by design and prefixed `bench_`.
    """
    from bench.dataset import load_all_cases
    from config import condor_path

    repo = condor_path()
    if repo is None or not (repo / "agents").is_dir():
        pytest.skip("no condor checkout — set CONDOR_PATH to enable this check")

    shipped = {p.name for p in (repo / "agents").iterdir() if p.is_dir()}
    bogus = sorted(
        {
            slug
            for c in load_all_cases()
            if (slug := getattr(c, "agent_slug", None))
            and not slug.startswith("bench_")
            and slug not in shipped
        }
    )
    assert not bogus, (
        f"cases name agent slugs condor does not ship: {bogus}. condor's roster is "
        f"{sorted(shipped)}. A slug that doesn't exist becomes a phantom routing "
        "domain with no config key."
    )


def test_specialist_cases_only_expect_tools_the_agent_is_granted():
    """An out-of-grant expectation is unpassable, not just optimistic.

    With allowed_tools live, a market_making_expert case is offered MM's 8 tools.
    Expecting `manage_trading_agent` — which MM does not declare — means the model
    is asked for a tool it cannot see: tool accuracy is 0 by construction and the
    row is then flagged as a harness artifact and dropped from routing. The domain
    silently loses a case instead of failing loudly.

    This is the authoring-time counterpart to the runtime `offered_tools` check in
    bench.scorer: catch it in the dataset, not in the results.
    """
    from bench.dataset import load_all_cases

    from config import condor_path

    if condor_path() is None:
        pytest.skip("no condor checkout — set CONDOR_PATH to enable this check")

    broken: dict[str, list[str]] = {}
    for case in load_all_cases():
        slug = getattr(case, "agent_slug", None)
        if not slug:
            continue
        grant = load_agent_tools(slug)
        if not grant:
            continue  # full-surface agent, or a bench synthetic with no AGENT.md
        expected = set(
            getattr(case, "expected_tools", None)
            or getattr(case, "expected_tool_calls", None)
            or []
        )
        outside = sorted(expected - set(grant))
        if outside:
            broken[case.id] = outside

    assert not broken, (
        "cases expect tools their agent is not granted, so the model can never "
        f"call them: {broken}. Either rewrite the case against the grant, or check "
        "whether condor's AGENT.md tools: list actually changed."
    )
