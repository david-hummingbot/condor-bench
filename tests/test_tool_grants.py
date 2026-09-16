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
    # Compared against the recorded surface, not a literal. The bound used to be
    # `< 24`, which was the surface when it was written; the surface is 44 tools at
    # condor d5eab53e, so the literal had stopped asserting anything about scoping.
    surface = _tool_surface_names()
    assert 0 < len(grant) < len(surface), (
        f"expected a scoped grant, got {len(grant)} of a {len(surface)}-tool surface"
    )
    assert "get_prices" in grant
    assert set(grant) <= surface, (
        f"grant names tools that are not in the recorded surface: "
        f"{sorted(set(grant) - surface)}"
    )


def _tool_surface_names() -> set[str]:
    """Every tool name in the recorded production surface, both servers."""
    import json

    from config import DATASETS_DIR

    snapshot = json.loads((DATASETS_DIR / "tool_surface.json").read_text())
    return {
        tool
        for spec in snapshot.get("servers", {}).values()
        for tool in (spec.get("tools") or {})
    }


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


# ── The per-mode cap vs the agent's grant ─────────────────────────────────────
# An allowlist is condor's own curated grant — already how production keeps the
# schema count down. Truncating it further by *position* threw away tools the
# agent is defined by for no reduction the grant had not already achieved: six
# agent-scoped cases were recorded as "expected tool was never offered" while
# their grant fit inside the cap the whole time.
#
# The grants have since outgrown the cap (16 / 13 / 20 at condor d5eab53e, all
# over moderate's 12), so "it fits anyway" no longer covers the same ground and
# went quiet when it stopped. What holds the guarantee now is the priority pass:
# an unavoidable cut falls on tools the case is not scored on, so the row stays a
# measurement of the model rather than of the harness. Counts stay out of these
# comments on purpose — the tests below construct their own grants.

import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from condor_compat.acp.pydantic_ai_client import PydanticAIClient  # noqa: E402

_ALL_TOOLS = sorted(
    [
        "configure_server", "delegate", "explore_dex_pools",
        "explore_geckoterminal", "get_available_models", "get_prices",
        "get_portfolio_overview", "manage_amm", "manage_bots",
        "manage_controllers", "list_executors", "manage_memory",
        "manage_routines", "manage_servers", "manage_skill", "manage_agents",
        "search_history", "send_notification",
        "set_account_position_mode_and_leverage", "trading_agent_journal_read",
        "trading_agent_journal_write", "create_grid_executor", "stop_executor",
        "run_code",
    ]
)
# market_making_expert's grant is larger than this; the cap tests need a grant
# that already fits inside moderate (12). These 11 names are a stand-in with
# the same shape: several sort past a 12-of-24 positional cut of the full list.
_MM_GRANT = [
    "get_prices", "get_portfolio_overview", "manage_bots", "manage_controllers",
    "list_executors", "manage_memory", "manage_routines", "manage_skill",
    "manage_agents", "search_history", "trading_agent_journal_read",
]


def _prepared(
    allowed: list[str] | None,
    mode: str,
    priority: list[str] | None = None,
) -> tuple[list[str], bool]:
    client = PydanticAIClient.__new__(PydanticAIClient)
    client.allowed_tools = set(allowed) if allowed else None
    client.priority_tools = set(priority) if priority else None
    client.tool_filter_mode = mode
    client.model_name = "openai:local-26b"
    client.offered_tools = None
    client.tools_truncated = False
    defs = [SimpleNamespace(name=f"mcp__condor__{n}") for n in _ALL_TOOLS]
    asyncio.run(client._prepare_tools(None, defs))
    return client.offered_tools, client.tools_truncated


def test_a_grant_inside_the_cap_is_not_trimmed_further():
    """The bug: 11 granted tools cut to 12-of-24's alphabetical prefix."""
    offered, truncated = _prepared(_MM_GRANT, "moderate")
    assert sorted(offered) == sorted(_MM_GRANT)
    assert not truncated, "a grant that already fits the cap is not a truncation"


def test_every_granted_tool_survives_the_cap():
    """These are the tools the agent is *defined* by — none may be withheld."""
    offered, _ = _prepared(_MM_GRANT, "moderate")
    assert not set(_MM_GRANT) - set(offered)


def test_a_grant_over_the_cap_is_still_trimmed():
    """The cap is a real constraint for local models, not advisory."""
    offered, truncated = _prepared(_ALL_TOOLS, "essential")
    assert len(offered) == 6
    assert truncated


def test_a_cut_never_falls_on_the_tools_the_case_is_scored_on():
    """The regression that returned when the grants outgrew the cap.

    Every scoped specialist now grants more than moderate's 12, so the positional
    cut is live again for mid-size local models. A cut that takes the expected
    tool with it does not measure the model: the row is dropped as a harness
    artifact and the domain reads as thin coverage.
    """
    over_cap = sorted(_ALL_TOOLS)[:16]
    # A tool that loses the positional cut on its own, so the assertion is about
    # the priority pass and not about alphabetical luck.
    expected = [t for t in over_cap if t not in sorted(over_cap)[:12]]
    assert expected, "fixture no longer exercises a tool the cut would drop"

    offered, truncated = _prepared(over_cap, "moderate", priority=expected)
    assert len(offered) == 12, "the cap must still bound what the model sees"
    assert truncated, "a real cut still has to be recorded"
    assert not set(expected) - set(offered), (
        f"the cut withheld the tools the case is scored on: "
        f"{sorted(set(expected) - set(offered))}"
    )


def test_priority_adds_nothing_that_the_grant_withheld():
    """Priority orders a cut; it is not a back door into the allowlist."""
    offered, _ = _prepared(_MM_GRANT, "moderate", priority=["manage_amm"])
    assert "manage_amm" not in offered, (
        "an ungranted tool became visible by being named as expected — the "
        "allowlist is production's scope and priority must not widen it"
    )


def test_an_uncut_run_keeps_discovery_order():
    """No cut, no reordering: nothing to justify perturbing the surface."""
    offered, truncated = _prepared(None, "full", priority=["stop_executor"])
    assert not truncated
    assert offered == [n for n in _ALL_TOOLS]


def test_chat_scoped_runs_still_hit_the_cap_and_say_so():
    """No grant means production offers all 24, so the cut is real — and flagged."""
    offered, truncated = _prepared(None, "moderate")
    assert len(offered) == 12
    assert truncated, "a real cut must be recorded, or thin coverage reads as failure"


def test_full_mode_offers_everything_untruncated():
    offered, truncated = _prepared(None, "full")
    assert len(offered) == len(_ALL_TOOLS)
    assert not truncated
