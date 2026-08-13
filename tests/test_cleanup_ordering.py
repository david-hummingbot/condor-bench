"""Teardown must undo in reverse, and must not read a refusal as a removal.

`agent_condor_builder_002` creates a trading agent and then a strategy under it.
Undoing in creation order attempted `delete_agent` first, and condor refuses that
while the agent still owns a strategy:

    {"error": "Agent 'bench_dca_agent' still owns 1 strategy(ies).
               Delete its strategies first."}

Two separate defects turned that into a silent leak. The order meant the agent could
never be deleted, and `tool_error` did not recognise the refusal — no `isError`, and
the text does not match the "unknown action" spelling it already checked for — so the
refusal was recorded in `removed`. With nothing in `failed` or `manual` the report was
`clean`, the dashboard emits a cleanup event only when it is not, and `bench_dca_agent`
stayed in the condor checkout until the roster drift check tripped over it.

The trace and response bodies below are the real ones from that run.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from bench.cleanup import created_resources, teardown, tool_error


class _Block:
    def __init__(self, text: str) -> None:
        self.text = text


class _Outcome:
    """Shaped like an MCP CallToolResult: content blocks, no exception."""

    def __init__(self, payload: dict) -> None:
        self.content = [_Block(json.dumps(payload))]
        self.isError = False


class _Result:
    """The subset of BenchmarkResult that cleanup reads."""

    def __init__(self, calls, responses) -> None:
        self.tool_calls = calls
        self.tool_responses = responses


def _builder_002_trace():
    calls = [
        {
            "tool": "mcp__condor__manage_trading_agent",
            "tool_call_id": "call_agent",
            "args": {
                "action": "create_agent",
                # Display name only — the slug is assigned server-side.
                "name": "Bench DCA Agent",
                "description": "Systematic DCA accumulation agent",
                "agent_key": "openrouter:anthropic/claude-haiku-4.5",
                "tools": [],
            },
        },
        {
            "tool": "mcp__condor__manage_trading_agent",
            "tool_call_id": "call_strategy",
            "args": {
                "action": "create_strategy",
                "agent_slug": "bench_dca_agent",
                "name": "bench_dca_sol",
                "description": "DCA accumulation for SOL-USDT on Binance",
            },
        },
    ]
    responses = [
        {
            "tool": "mcp__condor__manage_trading_agent",
            "tool_call_id": "call_agent",
            "output": json.dumps(
                {
                    "created": True,
                    "agent_slug": "bench_dca_agent",
                    "name": "Bench DCA Agent",
                }
            ),
        },
        {
            "tool": "mcp__condor__manage_trading_agent",
            "tool_call_id": "call_strategy",
            "output": json.dumps(
                {
                    "created": True,
                    # Namespaced under the agent, which is what delete_strategy wants.
                    "strategy_id": "bench_dca_agent.bench_dca_sol",
                    "name": "bench_dca_sol",
                }
            ),
        },
    ]
    return _Result(calls, responses)


def _condor_stub():
    """Stands in for condor: delete_agent refuses while a strategy is still owned."""
    state = {"strategies": {"bench_dca_agent.bench_dca_sol"}, "agents": {"bench_dca_agent"}}
    order: list[tuple[str, str]] = []

    async def call_tool(tool, args, *, agent_slug=None, model=""):
        action = args.get("action")
        order.append((action, args.get("agent_slug") or args.get("strategy_id") or ""))
        if action == "delete_strategy":
            sid = args.get("strategy_id")
            if sid not in state["strategies"]:
                return _Outcome({"deleted": False})
            state["strategies"].discard(sid)
            return _Outcome({"deleted": True})
        if action == "delete_agent":
            slug = args.get("agent_slug")
            if slug not in state["agents"]:
                # The bug this also guards: a wrong identifier is not an error, just
                # a delete that matched nothing.
                return _Outcome({"deleted": False})
            owned = [s for s in state["strategies"] if s.startswith(f"{slug}.")]
            if owned:
                return _Outcome(
                    {
                        "error": f"Agent '{slug}' still owns {len(owned)} strategy(ies). "
                        "Delete its strategies first."
                    }
                )
            state["agents"].discard(slug)
            return _Outcome({"deleted": True})
        raise AssertionError(f"unexpected undo call: {tool} {args}")

    return call_tool, state, order


def test_identifiers_come_from_the_response_not_the_display_name():
    """The slug is server-assigned; `name` is a human label that deletes nothing."""
    found = created_resources(_builder_002_trace())
    by_action = {r.action: r for r in found}
    assert by_action["create_agent"].identifier == "bench_dca_agent", (
        "picked the display name — delete_agent would have matched no agent"
    )
    assert by_action["create_strategy"].identifier == "bench_dca_agent.bench_dca_sol"


def test_undo_runs_child_before_parent(monkeypatch: pytest.MonkeyPatch):
    import bench.cleanup as cleanup

    call_tool, state, order = _condor_stub()
    monkeypatch.setattr(cleanup, "_call_tool", call_tool)

    report = asyncio.run(teardown(_builder_002_trace(), "test-model"))

    assert [a for a, _ in order] == ["delete_strategy", "delete_agent"], (
        f"undo ran in creation order ({order}) — delete_agent is refused while the "
        "agent still owns a strategy"
    )
    assert state["agents"] == set(), "the agent survived teardown"
    assert state["strategies"] == set()
    assert len(report.removed) == 2
    assert report.failed == []
    assert report.manual == []
    assert report.clean


def test_a_refusal_is_not_a_removal(monkeypatch: pytest.MonkeyPatch):
    """Force the old order and prove the failure is now visible rather than silent."""
    import bench.cleanup as cleanup

    call_tool, state, _ = _condor_stub()
    monkeypatch.setattr(cleanup, "_call_tool", call_tool)
    # Undo only the agent, with its strategy still in place — the exact call the old
    # ordering made first.
    trace = _builder_002_trace()
    trace.tool_calls = trace.tool_calls[:1]
    trace.tool_responses = trace.tool_responses[:1]

    report = asyncio.run(teardown(trace, "test-model"))

    assert report.removed == [], "a refused delete was recorded as a removal"
    assert len(report.failed) == 1
    assert "still owns" in report.failed[0]["error"]
    assert not report.clean, (
        "a clean report is why this leaked silently — the dashboard only emits a "
        "cleanup event when the report is not clean"
    )
    assert state["agents"] == {"bench_dca_agent"}


@pytest.mark.parametrize(
    "payload, expect_error",
    [
        ({"error": "Agent 'x' still owns 1 strategy(ies)."}, True),
        ({"deleted": False}, True),
        ({"removed": False}, True),
        ({"deleted": True}, False),
        ({"created": True, "agent_slug": "bench_dca_agent"}, False),
        # Absence is not failure, and a null/empty error field is not an error.
        ({"ok": True}, False),
        ({"error": None}, False),
        ({"error": ""}, False),
    ],
)
def test_tool_error_reads_condors_result_shapes(payload, expect_error):
    got = tool_error(_Outcome(payload))
    assert bool(got) is expect_error, f"{payload} -> {got!r}"


def test_the_leverage_reset_avoids_condors_broken_enum():
    """Leverage is real account state with no delete, and the reset always failed.

    It sent `position_mode: "ONEWAY"`, which condor's MCP layer rejects — it accepts only
    `HEDGE` / `ONE-WAY`, while the API behind it demands `HEDGE` / `ONEWAY`. No value
    satisfies both, so every reset errored ("left behind:
    set_account_position_mode_and_leverage BTC-USDT — Unknown") and leverage ratcheted
    across runs — the exact failure this state-setter machinery exists to prevent.

    `position_mode` is optional on the tool ("If position mode is not specified, will
    only set the leverage"), so dropping it makes the reset work without waiting for the
    condor bug to be fixed.
    """
    from bench.cleanup import _STATE_SETTERS, _undo_args, created_resources

    reset = _STATE_SETTERS["set_account_position_mode_and_leverage"]
    assert reset["leverage"] == 1
    assert "position_mode" not in reset, (
        "no position_mode value is accepted by both condor and the API"
    )

    class _Result:
        tool_calls = [
            {
                "tool": "mcp__mcp-hummingbot__set_account_position_mode_and_leverage",
                "tool_call_id": "c1",
                "args": {
                    "account_name": "master_account",
                    "connector_name": "binance_perpetual",
                    "trading_pair": "BTC-USDT",
                    "leverage": 20,
                    "position_mode": "HEDGE",
                },
            }
        ]
        tool_responses = []

    found = created_resources(_Result())
    assert len(found) == 1
    args = _undo_args(found[0], "set")
    assert args["leverage"] == 1
    assert "position_mode" not in args, "the reset must not resend the rejected enum"
    # Scoping args carry through, so the reset lands on the pair the case changed.
    assert args["trading_pair"] == "BTC-USDT"
    assert args["connector_name"] == "binance_perpetual"
