"""Unit tests for scoring / tool accuracy fixes."""
from __future__ import annotations

from pathlib import Path

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
