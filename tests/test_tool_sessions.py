"""Direct MCP calls (teardown, post-condition probes) boot each server once per batch."""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import pytest


@pytest.fixture
def fake_mcp(monkeypatch):
    import mcp
    import mcp.client.stdio

    import bench.cleanup as cleanup

    state = {"boots": [], "calls": [], "fail_next": False, "fail_close": False}

    @contextlib.asynccontextmanager
    async def stdio_client(params):
        state["boots"].append(params)
        yield ("r", "w")
        if state["fail_close"]:
            raise RuntimeError("server did not exit cleanly")

    class ClientSession:
        def __init__(self, read, write):
            self.n = len(state["boots"])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def initialize(self):
            return None

        async def call_tool(self, tool, args):
            if state["fail_next"]:
                state["fail_next"] = False
                raise BrokenPipeError("server went away")
            state["calls"].append((self.n, tool))
            return SimpleNamespace(content=[], is_error=False)

    monkeypatch.setattr(mcp.client.stdio, "stdio_client", stdio_client)
    monkeypatch.setattr(mcp, "ClientSession", ClientSession)
    monkeypatch.setattr(cleanup, "_server_params", lambda target, **kw: target)
    return state


def test_a_batch_boots_each_server_once(fake_mcp):
    from bench.cleanup import _call_tool, tool_sessions

    async def go():
        async with tool_sessions(agent_slug="a"):
            await _call_tool("manage_loops", {}, agent_slug="a", model="m")
            await _call_tool("manage_agents", {}, agent_slug="a", model="m")
            await _call_tool("stop_executor", {}, agent_slug="a", model="m")

    asyncio.run(go())
    assert fake_mcp["boots"] == ["condor", "mcp-hummingbot"]
    assert len(fake_mcp["calls"]) == 3


def test_a_call_with_other_wiring_does_not_borrow_the_batch(fake_mcp):
    """A server booted for one agent_slug writes that agent's stores only."""
    from bench.cleanup import _call_tool, tool_sessions

    async def go():
        async with tool_sessions(agent_slug="a"):
            await _call_tool("manage_loops", {}, agent_slug="a", model="m")
            await _call_tool("manage_loops", {}, agent_slug="b", model="m")
            await _call_tool("manage_loops", {}, agent_slug="a", model="m", tick=True)

    asyncio.run(go())
    assert len(fake_mcp["boots"]) == 3


def test_outside_a_batch_every_call_boots_its_own_server(fake_mcp):
    from bench.cleanup import _call_tool

    async def go():
        await _call_tool("manage_loops", {}, agent_slug=None, model="m")
        await _call_tool("manage_loops", {}, agent_slug=None, model="m")

    asyncio.run(go())
    assert len(fake_mcp["boots"]) == 2


def test_a_broken_server_is_replaced_not_reused(fake_mcp):
    from bench.cleanup import _call_tool, tool_sessions

    async def go():
        async with tool_sessions(agent_slug=None):
            fake_mcp["fail_next"] = True
            with pytest.raises(BrokenPipeError):
                await _call_tool("manage_loops", {}, agent_slug=None, model="m")
            await _call_tool("manage_loops", {}, agent_slug=None, model="m")

    asyncio.run(go())
    assert len(fake_mcp["boots"]) == 2
    assert fake_mcp["calls"] == [(2, "manage_loops")], "the retry went to the fresh server"


def test_a_server_that_fails_to_close_does_not_fail_the_batch(fake_mcp):
    """teardown and verify promise never to raise."""
    from bench.post_conditions import verify

    fake_mcp["fail_close"] = True
    rows = asyncio.run(verify({"manage_routines": {"action": "list"}}, model="m"))
    assert rows and rows[0]["reachable"] is True
