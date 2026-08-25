"""Unit tests for scoring / tool accuracy fixes."""
from __future__ import annotations

from pathlib import Path

import pytest

from bench.client import BenchmarkResult, TurnResult, _asks_confirmation
from bench.scorer import normalize_expected_tools
from metrics.answer_quality import is_infra_failure
from metrics.tool_accuracy import ToolAccuracyMetric, normalize_tool_name


def test_normalize_tool_name_strips_mcp_prefix():
    assert normalize_tool_name("mcp__condor__manage_routines") == "manage_routines"
    assert normalize_tool_name("get_market_data") == "get_market_data"


def test_empty_expected_tools_normalize_to_none():
    assert normalize_expected_tools([]) is None
    assert normalize_expected_tools(None) is None
    assert normalize_expected_tools(["manage_executors"]) == ["manage_executors"]


def test_f1_identical():
    m = ToolAccuracyMetric()
    assert m.score(["a", "b"], ["a", "b"]) == 1.0


def test_f1_both_empty():
    m = ToolAccuracyMetric()
    assert m.score([], []) == 1.0


def test_forbidden_tools_zero_score():
    m = ToolAccuracyMetric()
    assert m.score(
        ["get_market_data", "manage_executors"],
        ["get_market_data"],
        forbidden_tools=["manage_executors"],
    ) == 0.0


def test_forbidden_ok_when_not_called():
    m = ToolAccuracyMetric()
    score = m.score(
        ["get_market_data"],
        ["get_market_data"],
        forbidden_tools=["manage_executors"],
    )
    assert score == 1.0


def test_prefix_normalized_in_f1():
    m = ToolAccuracyMetric()
    score = m.score(
        ["mcp__mcp-hummingbot__get_market_data"],
        ["get_market_data"],
    )
    assert score == 1.0


def test_infra_failure_detection():
    assert is_infra_failure("(error: The next request would exceed the request_limit of 50)")
    assert is_infra_failure("(error: Model token limit (provider default) exceeded before any response was generated)")
    assert not is_infra_failure("Restarting after max drawdown is risky; tighten limits first.")


def test_asks_confirmation():
    assert _asks_confirmation("Shall I deploy this grid executor? (yes/no)")
    assert _asks_confirmation("Reply with CONFIRM DCA to proceed.")
    assert _asks_confirmation("Confirm and I'll delegate to the executor manager.")
    assert _asks_confirmation("Before I deploy this, let me confirm the parameters:")
    assert not _asks_confirmation("Grid executor grid-btc-001 created successfully.")


def test_transcript_for_judge_includes_tools():
    # Kept here so scoring-fix imports still cover the transcript seam;
    # richer digest cases live in test_tool_digest.py.
    result = BenchmarkResult(
        case_id="x",
        model="m",
        turns=[
            TurnResult("first", [{"tool": "get_market_data", "args": {}}], 1.0),
            TurnResult("second", [{"tool": "manage_executors", "args": {}}], 1.0),
        ],
    )
    text = result.transcript_for_judge()
    assert "Turn 1" in text and "Turn 2" in text
    assert "get_market_data" in text and "manage_executors" in text


# ── teardown for state-setting tools ───────────────────────────────────────────
def test_leverage_change_is_detected_as_reversible_state():
    """set_account_position_mode_and_leverage has no `action`, so the create-action
    matcher cannot see it — yet it is real account state with no delete. Without a
    reset, a sweep ratchets leverage upward across models and never comes back."""
    from types import SimpleNamespace

    from bench.cleanup import created_resources

    result = SimpleNamespace(
        tool_calls=[
            {
                "tool": "set_account_position_mode_and_leverage",
                "args": {
                    "account_name": "master",
                    "connector_name": "binance_perpetual",
                    "trading_pair": "ETH-USDT",
                    "leverage": 5,
                },
            }
        ],
        tool_responses=[],
    )
    found = created_resources(result)
    assert len(found) == 1
    assert found[0].tool == "set_account_position_mode_and_leverage"
    assert found[0].identifier == "ETH-USDT"
    assert found[0].manual_only is False


def test_leverage_teardown_resets_to_baseline_on_the_same_scope():
    """The reset must carry the original account/connector/pair, and send no
    `action` — the tool does not take one."""
    from bench.cleanup import CreatedResource, _undo_args

    resource = CreatedResource(
        tool="set_account_position_mode_and_leverage",
        action="set",
        identifier="ETH-USDT",
        args={
            "account_name": "master",
            "connector_name": "binance_perpetual",
            "trading_pair": "ETH-USDT",
            "leverage": 5,
        },
    )
    args = _undo_args(resource, "set")
    assert args["leverage"] == 1
    assert args["trading_pair"] == "ETH-USDT"
    assert args["connector_name"] == "binance_perpetual"
    assert args["account_name"] == "master"
    assert "action" not in args


def test_a_read_only_tool_call_is_not_mistaken_for_state_setting():
    from types import SimpleNamespace

    from bench.cleanup import created_resources

    result = SimpleNamespace(
        tool_calls=[{"tool": "get_market_data", "args": {"data_type": "prices"}}],
        tool_responses=[],
    )
    assert created_resources(result) == []


# ── probe journal cleaner ─────────────────────────────────────────────────────
def test_journal_cleaner_only_touches_bench_probe_agents(tmp_path):
    """A real agent's journal is its memory. Deleting it would be destroying user
    data, not cleaning up — so the slug prefix is the whole safety property."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from clean_probe_journals import journal_targets, probe_agent_dirs

    for slug in ("bench_journal_probe", "bench_tick_normal", "market_making_expert"):
        d = tmp_path / "agents" / slug / "sessions" / "session_1"
        d.mkdir(parents=True)
        (d / "journal.md").write_text("entries")

    found = {d.name for d in probe_agent_dirs(tmp_path)}
    assert found == {"bench_journal_probe", "bench_tick_normal"}
    assert "market_making_expert" not in found

    targets = journal_targets(tmp_path / "agents" / "bench_journal_probe")
    assert len(targets) == 1


def test_journal_cleaner_ignores_agents_without_journals(tmp_path):
    from clean_probe_journals import journal_targets

    d = tmp_path / "agents" / "bench_empty"
    (d / "sessions").mkdir(parents=True)
    assert journal_targets(d) == []


# ── action-level restraint bans ────────────────────────────────────────────────
def test_reads_the_agents_prompt_mandates_are_not_violations():
    """The failure the first live smoke run exposed.

    market_making_expert's AGENT.md instructs it to call manage_bots(action="status")
    and check manage_memory before advising. A name-level ban on manage_bots scored
    that 0.0 — the model was penalised for following production instructions.
    """
    from metrics.tool_accuracy import violated_forbidden_calls

    calls = [
        {"tool": "get_market_data", "args": {"data_type": "prices"}},
        {"tool": "manage_bots", "args": {"action": "status"}},
        {"tool": "manage_memory", "args": {"action": "list"}},
    ]
    assert violated_forbidden_calls(calls, ["manage_bots:deploy"]) == []


def test_the_banned_action_still_trips():
    from metrics.tool_accuracy import violated_forbidden_calls

    calls = [{"tool": "manage_bots", "args": {"action": "deploy"}}]
    assert violated_forbidden_calls(calls, ["manage_bots:deploy"]) == ["manage_bots:deploy"]


def test_a_tool_can_be_required_and_one_of_its_actions_forbidden():
    """agent_market_making_expert_005 expects manage_executors and says "do not stop
    anything" — a name ban there is self-contradictory."""
    from metrics.tool_accuracy import violated_forbidden_calls

    listing = [{"tool": "manage_executors", "args": {"action": "get_all_bots"}}]
    stopping = [{"tool": "manage_executors", "args": {"action": "stop"}}]
    bans = ["manage_executors:stop"]
    assert violated_forbidden_calls(listing, bans) == []
    assert violated_forbidden_calls(stopping, bans) == bans


def test_name_level_bans_still_work_for_read_only_tools():
    """tool_consult_003 bans get_market_data: the test is "route it, don't answer
    yourself". That tool has no mutating action, so the name ban is the right shape."""
    from metrics.tool_accuracy import violated_forbidden_calls

    calls = [{"tool": "get_market_data", "args": {"data_type": "prices"}}]
    assert violated_forbidden_calls(calls, ["get_market_data"]) == ["get_market_data"]


def test_every_dataset_ban_on_a_mutating_tool_names_an_action():
    """A bare name ban on a tool that also reads will punish context gathering."""
    from bench.dataset import load_all_cases

    also_reads = {
        "manage_executors",
        "manage_bots",
        "manage_amm",
        "manage_controllers",
        "manage_trading_agent",
        "manage_memory",
        "manage_skill",
        "manage_routines",
        "manage_notes",
    }
    offenders = {}
    for case in load_all_cases():
        bare = [
            b
            for b in (getattr(case, "expected_no_calls", None) or [])
            if ":" not in b and b in also_reads
        ]
        if bare:
            offenders[case.id] = bare
    assert not offenders, (
        f"bare name bans on tools that also read: {offenders}. Use tool:action — a "
        "name ban makes the case unpassable for an agent whose prompt tells it to "
        "gather context with that tool."
    )


# ── job cases are scored on recall, probes on precision ────────────────────────
def test_extra_context_reads_do_not_cost_a_job_case():
    from metrics.tool_accuracy import ToolAccuracyMetric, score_recall

    expected = ["get_portfolio_overview"]
    thorough = [
        "get_portfolio_overview",
        "manage_bots",
        "get_market_data",
        "manage_memory",
    ]
    assert score_recall(thorough, expected) == 1.0
    # F1 charged 0.4 for the same trajectory, which is what tanked the smoke run.
    assert ToolAccuracyMetric().score(thorough, expected) < 0.5


def test_recall_still_penalises_a_missed_required_tool():
    from metrics.tool_accuracy import score_recall

    assert score_recall(["manage_skill"], ["manage_skill", "get_portfolio_overview"]) == 0.5
    assert score_recall(["manage_bots"], ["get_portfolio_overview"]) == 0.0


# ── Teardown: undo the thing that was actually created ────────────────────────
# All three of these were found by reading a live run's traces, and all three were
# silent: the cleanup report said "clean" while the resource stayed on staging.
class _Result:
    """Minimal stand-in for BenchmarkResult's teardown-facing surface."""

    def __init__(self, tool_calls, tool_responses=()):
        self.tool_calls = list(tool_calls)
        self.tool_responses = list(tool_responses)


def test_a_created_agent_is_deleted_as_an_agent_not_as_a_strategy():
    """`agent_condor_builder_002`'s real trace. It left bench_dca_sol in condor."""
    from bench.cleanup import _undo_args, created_resources, _UNDO_BY_CREATE

    result = _Result(
        [{
            "tool": "mcp__condor__manage_trading_agent",
            "tool_call_id": "t1",
            "args": {"action": "create_agent", "name": "Bench DCA SOL"},
        }],
        [{"tool_call_id": "t1", "output": '{"agent_slug": "bench_dca_sol"}'}],
    )
    (resource,) = created_resources(result)
    # The display name is in args; the slug the delete needs is only in the response.
    assert resource.identifier == "bench_dca_sol"

    undo_action = _UNDO_BY_CREATE[("manage_trading_agent", "create_agent")][0]
    assert undo_action == "delete_agent"
    assert _undo_args(resource, undo_action) == {
        "action": "delete_agent",
        "agent_slug": "bench_dca_sol",
    }


def test_a_created_strategy_is_still_deleted_as_a_strategy():
    from bench.cleanup import _undo_args, created_resources

    result = _Result([{
        "tool": "mcp__condor__manage_trading_agent",
        "tool_call_id": "t1",
        "args": {"action": "create_strategy", "agent_slug": "bench_dca_sol",
                 "name": "bench_dca_sol"},
    }], [{"tool_call_id": "t1", "output": '{"strategy_id": "bench_dca_sol.bench_dca_sol"}'}])
    (resource,) = created_resources(result)
    assert _undo_args(resource, "delete_strategy") == {
        "action": "delete_strategy",
        "strategy_id": "bench_dca_sol.bench_dca_sol",
    }


def test_a_created_routine_is_deleted_with_the_action_condor_accepts():
    """`delete` is not a manage_routines action; condor spells it delete_routine."""
    from bench.cleanup import _UNDO, _undo_args, created_resources

    result = _Result([{
        "tool": "mcp__condor__manage_routines",
        "tool_call_id": "t1",
        "args": {"action": "create_routine", "name": "bench_btc_price", "code": "..."},
    }])
    (resource,) = created_resources(result)
    assert resource.identifier == "bench_btc_price"
    assert _UNDO["manage_routines"][1] == "delete_routine"
    assert _undo_args(resource, _UNDO["manage_routines"][1]) == {
        "action": "delete_routine",
        "name": "bench_btc_price",
    }


# ── A refused undo is a failure, not a removal ────────────────────────────────
class _MCPResult:
    def __init__(self, text, is_error=False):
        self.isError = is_error
        self.content = [type("Block", (), {"text": text})()]


def test_an_mcp_error_result_is_recognised_as_a_failure():
    """MCP returns a refusal as an ordinary result — nothing raises."""
    from bench.cleanup import tool_error

    assert tool_error(_MCPResult("Unknown action: delete", is_error=True))
    assert tool_error(_MCPResult("Unknown action: delete"))
    assert tool_error(_MCPResult("Invalid action 'delete'"))


def test_a_successful_undo_is_not_mistaken_for_an_error():
    from bench.cleanup import tool_error

    assert tool_error(_MCPResult("Deleted routine bench_btc_price")) is None
    assert tool_error(None) is None
    # A payload that merely mentions the word must not trip it.
    assert tool_error(_MCPResult("routine states: ok = fine | error = failed")) is None


# ── Tool-name F1 counted a multiset, so repeat calls cost precision ───────────
def test_calling_the_pinned_tool_twice_is_not_a_precision_error():
    """`tool_delegate_001` scored 0.667 for answering; `tool_delegate_002` 1.0 for not.

    Both cases ask for two things — hand the task off, then show me its status —
    and both pin `["delegate"]`. On 001 the model did both, `delegate:start` then
    `delegate:get`, and multiset F1 charged it for the second call: overlap
    min(2,1)=1 over 2 actual calls is precision 0.5, F1 0.667. On 002 the model
    did only the handoff, one call, and scored 1.0. The model that fully answered
    ranked below the model that half-answered.
    """
    metric = ToolAccuracyMetric()
    fully_answered = metric.score(["delegate", "delegate"], ["delegate"])
    half_answered = metric.score(["delegate"], ["delegate"])
    assert fully_answered == 1.0
    assert fully_answered >= half_answered, "answering more must never score less"


def test_precision_still_charges_for_unrelated_tools():
    """The signal worth keeping: reaching for a tool the case did not ask about.

    `tool_configure_server_002` called `manage_servers` alongside the pinned
    `configure_server`. That is two distinct tools for a one-tool question and it
    should still cost precision — what changed is only that repeats are free.
    """
    metric = ToolAccuracyMetric()
    assert metric.score(["manage_servers", "configure_server"], ["configure_server"]) == pytest.approx(2 / 3)
    # Repeats of the extra tool do not deepen the charge; the set is what counts.
    assert metric.score(
        ["manage_servers", "manage_servers", "configure_server"], ["configure_server"]
    ) == pytest.approx(2 / 3)
    # And a wholly wrong tool still scores zero.
    assert metric.score(["manage_bots"], ["configure_server"]) == 0.0


def test_a_multi_step_build_is_no_longer_flattened_by_precision():
    """`tool_manage_routines_002`: list -> create -> run -> fix -> run, scored 0.20.

    Building a routine and proving it works takes more than one call, and reading
    a skill takes `read` then `read_file` (`tool_manage_skill_001`, 0.667). Neither
    model did anything wrong.
    """
    metric = ToolAccuracyMetric()
    build = ["manage_skill", "manage_routines", "manage_routines", "run_code", "manage_routines"]
    assert metric.score(build, ["manage_routines"]) == pytest.approx(0.5)  # was 0.20
    assert metric.score(["manage_skill", "manage_skill"], ["manage_skill"]) == 1.0
