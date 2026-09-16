"""AGENT.md must match the system prompt condor actually ships.

bench/client.py injects condor_compat/agents/condor/AGENT.md verbatim as the
Condor system prompt. When condor edits its prompt and this copy doesn't follow,
the benchmark grades models against rules production no longer states — and the
tool-accuracy metric in particular starts measuring the wrong behaviour.

This check needs a condor checkout, so it skips when there isn't one (CI without
the sibling repo, a fresh clone). It is a guard for developers who have both
repos side by side, not a hard gate.

``agents/prompts.py``, the chat preload and the two profile modules are no longer
in that category: ``scripts/revendor.py`` generates them, and
``test_vendored_prompts_are_not_stale`` below byte-compares the generated output.
Only ``acp/`` still carries hand-made bench edits.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VENDORED = ROOT / "condor_compat" / "agents" / "condor" / "AGENT.md"
UPSTREAM_REL = Path("agents") / "condor" / "AGENT.md"


def _strip_frontmatter(text: str) -> str:
    """Drop the leading YAML block; the vendored copy is body-only."""
    if text.startswith("---"):
        return text.split("---", 2)[2].lstrip("\n")
    return text


def _condor_repo() -> Path | None:
    """The same checkout every other part of bench resolves.

    This used to read CONDOR_REPO directly while ``config.condor_path()`` accepted
    CONDOR_PATH as well. With more than one condor clone on a machine that is a
    way to validate the vendored prompt against one checkout while live runs use
    another — so resolution goes through one function.
    """
    from config import condor_path

    candidate = condor_path()
    return candidate if candidate and (candidate / UPSTREAM_REL).is_file() else None


def test_agent_md_matches_condor():
    repo = _condor_repo()
    if repo is None:
        pytest.skip(
            "no condor checkout — set CONDOR_PATH to the condor repo root to enable "
            "this check"
        )

    upstream = _strip_frontmatter((repo / UPSTREAM_REL).read_text())
    vendored = VENDORED.read_text()

    if upstream.strip() == vendored.strip():
        return

    import difflib

    diff = "\n".join(
        difflib.unified_diff(
            upstream.splitlines(),
            vendored.splitlines(),
            fromfile=f"condor/{UPSTREAM_REL}",
            tofile=str(VENDORED.relative_to(ROOT)),
            lineterm="",
            n=1,
        )
    )
    pytest.fail(
        "Vendored AGENT.md has drifted from condor's system prompt.\n"
        "Re-vendor it (frontmatter stripped) so benchmarks grade against the "
        f"rules production states.\n"
        f"Compared against: {repo}  (set CONDOR_PATH if that is the wrong "
        f"checkout)\n\n{diff}"
    )


# ── The tick prompt's tool names ───────────────────────────────────────────────
# `condor_compat/agents/prompts.py` cannot be byte-compared with upstream (it
# carries deliberate bench edits), which is how it came to name `manage_executors`
# and `get_market_data` for a whole commit after condor had replaced both. Nothing
# failed: the tick prompt is not scored, so the names it states just quietly
# contradicted `expected_tool_calls` — and the ToolSearch preload line, which is
# resolved against the mounted surface, missed on every stale name and left the
# session holding no tools at all.
#
# So the one thing that CAN be checked is checked: every MCP tool the prompt names
# has to exist in the recorded surface. That is the half of re-vendoring a machine
# can do.

TICK_PROMPT = ROOT / "condor_compat" / "agents" / "prompts.py"

# Words that look like tool names in prose but are not tools. Kept explicit and
# short: a growing list here would mean the pattern below is too loose.
_NOT_TOOLS = frozenset(
    {
        # The tool that was removed and whose absence the rules state.
        "place_order",
        # A generic mention, always alongside the five real names.
        "create_executor",
    }
)


def _surface_names() -> set[str]:
    import json

    from config import DATASETS_DIR

    snapshot = json.loads((DATASETS_DIR / "tool_surface.json").read_text())
    return {
        tool
        for spec in snapshot.get("servers", {}).values()
        for tool in (spec.get("tools") or {})
    }


def test_every_tool_the_tick_prompt_names_exists():
    """A stale name here is a tick case that cannot pass, and nothing else says so."""
    import re

    text = TICK_PROMPT.read_text()
    surface = _surface_names()

    # Two shapes: a bare call (`get_prices(...)`, `manage_skill(action=`) and the
    # namespaced preload form (`mcp__condor__run_code`). Both are places a name has
    # to be real; prose that merely mentions a family ("the create_*_executor
    # tools") is not matched, since it names no single tool.
    named = set(re.findall(r"mcp__[a-z-]+__([a-z_]+)", text))
    named |= set(re.findall(r"\b([a-z][a-z_]{4,})\(action=", text))
    named |= set(re.findall(r"\b(create_[a-z]+_executor|stop_executor|list_executors)\b", text))
    named -= _NOT_TOOLS

    unknown = sorted(named - surface)
    assert not unknown, (
        f"the vendored tick prompt names tools that are not in the surface: "
        f"{unknown}. Re-vendor those lines from condor's own prompts.py — a tick "
        "case scored on expected_tool_calls cannot pass a prompt that sends the "
        "model at a tool condor does not mount, and the ToolSearch preload line "
        "silently resolves to nothing."
    )
    # Guard the guard: a pattern that matches nothing would pass forever.
    assert len(named) >= 10, (
        f"only matched {sorted(named)} — the extraction patterns no longer find "
        "the prompt's tool names, so this check is vacuous"
    )


def test_the_tick_preload_offers_the_tools_the_tick_cases_expect():
    """Every tool a tick case is scored on has to be in the preload line.

    The preload is one ToolSearch call made before the model does anything, so a
    tool missing from it is a tool the tick has to go discover mid-turn — which is
    the failure mode the line exists to prevent.
    """
    from bench.dataset import load_all_cases
    from condor_compat.agents.prompts import _build_tool_preload

    live = _build_tool_preload(is_dry_run=False, is_experiment=False)
    expected: set[str] = set()
    for case in load_all_cases():
        if getattr(case, "type", "") != "tick":
            continue
        expected.update(getattr(case, "expected_tool_calls", None) or [])

    missing = sorted(t for t in expected if f"__{t}" not in live)
    assert not missing, (
        f"tick cases are scored on {missing}, which the preload does not load. "
        "Either the case or the vendored preload is wrong — check condor's "
        "prompts.py for which."
    )


# ── The prompt's TOOLS section must match the client bench builds ─────────────
# `build_tick_prompt` picks that section from `config["agent_key"]`: a pydantic-ai
# key is told "all MCP tools are pre-loaded", anything else gets a ToolSearch
# preload line, because on the ACP path tools stay deferred until the agent asks
# (bench/client.call_origin says so: "Claude Code answers a 24-tool prompt by
# first calling its own ToolSearch"). Bench reads the model from the CLI and the
# config from a fixture, so reading the fixture inverted the dependency — every
# tick case pins `anthropic:claude-sonnet-4-6`, so a claude-code run was told its
# tools were loaded when they were not.

_TICK_CASE_ID = "t001"


def _tick_case():
    from bench.dataset import load_all_cases

    for case in load_all_cases():
        if case.id == _TICK_CASE_ID:
            return case
    raise AssertionError(f"{_TICK_CASE_ID} is gone — repoint this test at a tick case")


# The two shapes a bench model key comes in. A bare id ("gemini-2.5-pro") is not a
# model key at all — it is what an ACP agent reports for itself once resolved, per
# the note in `estimate_cost_usd` — so it is deliberately not enumerated here.
_ACP_KEYS = ("claude-code", "gemini")
_PYDANTIC_KEYS = (
    "ollama:qwen2.5:7b",
    "openai:qwen/qwen3.6-27b",
    "anthropic:claude-opus-5",
    "openrouter:anthropic/claude-sonnet-5",
    "lmstudio:local-model",
    "groq:llama-3.3-70b-versatile",
    "google:gemini-2.5-pro",
    "custom@venice:llama-3.3-70b",
)

_PRELOADED_CLAIM = "All MCP tools are pre-loaded and available"


def test_the_prompt_tools_section_follows_the_model_under_test():
    """Not the dataset's pinned agent_key, which is a fixture, not a decision."""
    from bench.client import build_tick_prompt_for_case
    from condor_compat.acp.acp_client import is_acp_model

    case = _tick_case()
    pinned = case.config.get("agent_key")
    assert pinned, "the case no longer pins an agent_key — this test guards the override"

    for model in _ACP_KEYS + _PYDANTIC_KEYS:
        prompt = build_tick_prompt_for_case(case, model)
        has_preload = "ToolSearch(query=" in prompt
        claims_preloaded = _PRELOADED_CLAIM in prompt
        if is_acp_model(model):
            assert has_preload, (
                f"{model} runs on the ACP client, where tools are deferred, but its "
                "tick prompt carries no ToolSearch preload line — it will be told "
                f"the tools are loaded when they are not (pin: {pinned})"
            )
            assert not claims_preloaded, f"{model}: ACP prompt claims pre-loaded tools"
        else:
            assert claims_preloaded, (
                f"{model} runs on the PydanticAI client, which registers the tools "
                "in the request, but its prompt does not say so"
            )
            assert not has_preload, (
                f"{model}: prompt tells a PydanticAI run to call ToolSearch, which "
                "only the ACP path has — the tick would spend its turn on a tool "
                "that does not exist"
            )


def test_every_bench_model_key_shape_is_classified():
    """The predicate deciding the branch has to agree with the client bench builds.

    Bench has exactly two clients, so `not is_acp_model` *is* "PydanticAI". A key
    shape both predicates read as non-pydantic-ai and non-ACP would take the ACP
    prompt branch while running on the PydanticAI client. `custom@venice:…` was
    exactly that: the vendored `is_pydantic_ai_model` split on ":" without
    stripping the `@nickname`, so it matched nothing.
    """
    from condor_compat.acp.acp_client import is_acp_model
    from condor_compat.acp.pydantic_ai_client import (
        PYDANTIC_AI_PREFIXES,
        is_pydantic_ai_model,
        model_prefix,
    )

    for model in _PYDANTIC_KEYS:
        assert is_pydantic_ai_model(model), (
            f"{model} is run on the PydanticAI client but is not recognised as one; "
            f"prefix reads as {model_prefix(model)!r}, known: "
            f"{sorted(PYDANTIC_AI_PREFIXES)}"
        )
        assert not is_acp_model(model)
    for model in _ACP_KEYS:
        assert is_acp_model(model)
        assert not is_pydantic_ai_model(model), (
            f"{model} would take the pydantic-ai prompt branch while running on ACP"
        )


def test_a_controller_mode_tick_gets_the_controller_base_prompt():
    """`bot_name` in a tick config must select condor's controller base prompt.

    This test used to assert the opposite — that bench could not handle a
    controller-shaped tick, because the vendored copy carried the executor base
    and the config-driven [CONTROLLER MODE] section but not the controller base,
    so such a case got both halves of a contradiction: "trade ONLY via the
    create_*_executor tools" up top and "do NOT create standalone executors"
    further down. Its failure message said to vendor BASE_PROMPT_LIVE_CONTROLLER
    and pick the base from the surface the way upstream does. scripts/revendor.py
    now copies the whole file, so that is what happens, and the guard flips into
    a check that the right base is chosen.
    """
    from bench.client import build_tick_prompt_for_case

    case = _tick_case()
    controller = build_tick_prompt_for_case(
        type("C", (), {**case.__dict__, "config": {**case.config, "bot_name": "x"}})(),
        "anthropic:claude-opus-5",
    )
    executor = build_tick_prompt_for_case(case, "anthropic:claude-opus-5")

    assert "steering the controllers" in controller, (
        "a controller-mode tick did not get BASE_PROMPT_LIVE_CONTROLLER"
    )
    assert "Trade ONLY via the create_*_executor tools" not in controller, (
        "the executor base leaked into a controller-mode prompt — the "
        "contradiction this test was written about"
    )
    assert "Trade ONLY via the create_*_executor tools" in executor, (
        "a normal tick must still get the executor base"
    )



# ── The generated vendor must match the checkout ─────────────────────────────
# The check the old suite could not make. Its two preload tests compare tool
# *names* against the surface snapshot, so a vendored copy could lose whole
# paragraphs of prompt text and stay green — which is exactly what happened:
# BASE_PROMPT_COMMON was missing the [CORE DATA - drift] rules and the
# routine-authoring rule, and JOURNAL_SECTION_LIVE was missing the entire
# SESSION CANVAS block (the four sections, the journal_write call shape, the
# 1200-char cap). Nothing named a tool wrongly, so nothing failed.
#
# Now the files are generated, so "did someone edit the generated copy" and "did
# condor move" are the same question, and it is asked by regenerating.


def test_vendored_prompts_are_not_stale():
    """Regenerate into memory and compare. Skips without a condor checkout."""
    repo = _condor_repo()
    if repo is None:
        pytest.skip("no condor checkout — set CONDOR_PATH to enable this check")

    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from revendor import generate

    stale = [
        path.relative_to(ROOT)
        for path, text in generate(repo).items()
        if not path.exists() or path.read_text() != text
    ]
    assert not stale, (
        f"vendored copies are stale or hand-edited: {stale}. "
        "Re-pull with: uv run python scripts/revendor.py"
    )


def test_the_consult_preload_is_the_chat_seat_not_the_tick_seat():
    """A consult case must be opened with the 42-tool chat preload.

    The bug this pins: consult cases got no preload at all, and the only one
    vendored was the tick seat's — which condor says in as many words must not be
    unified with the chat one ("a tick must not be able to start or stop the loop
    it is running inside"). The tick ring omits 24 tools the chat seat mounts,
    `get_portfolio_overview` among them, so "how is my portfolio doing?" cost a
    second ToolSearch that production never pays.
    """
    from condor_compat.agents.prompts import _build_tool_preload
    from condor_compat.runtime.context import build_consult_preload

    chat = build_consult_preload(None, "claude-code:sonnet")
    tick = _build_tool_preload(is_dry_run=False, is_experiment=False)

    assert "get_portfolio_overview" in chat, "the chat preload must name it"
    assert "get_portfolio_overview" not in tick, (
        "the tick ring gained it — if condor unified the seats, drop this test; "
        "if not, the extraction is reading the wrong function"
    )
    assert _tool_count(chat) > _tool_count(tick), (
        "the chat seat mounts strictly more than the tick seat; a chat preload no "
        "larger than the tick one means build_consult_preload resolved the wrong ring"
    )


def test_a_consult_prompt_carries_the_preload_for_an_acp_model():
    """End to end, through the builder run_consult actually calls."""
    from bench.client import _build_consult_prompt

    acp = _build_consult_prompt("how is my portfolio doing?", "", agent_key="claude-code:sonnet")
    assert "ToolSearch" in acp and "get_portfolio_overview" in acp

    # pydantic-ai seats auto-discover and must never be told to preload.
    pai = _build_consult_prompt("same", "", agent_key="anthropic:claude-sonnet-4-6")
    assert "ToolSearch" not in pai


def _tool_count(preload: str) -> int:
    import re

    return len(re.findall(r"mcp__[a-z-]+__[a-z_]+", preload))
