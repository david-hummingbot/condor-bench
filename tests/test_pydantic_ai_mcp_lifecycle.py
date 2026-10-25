"""A post-startup MCP collapse must be loud and must kill the client (CORR-332).

Ported from condor 39cb7d50. bench's copy of ``_run_mcp_lifecycle`` was a step
behind condor's even before that fix: its handler recorded an exception only when
one fired *before* the ready event, so a post-startup failure was discarded with
nothing logged and ``self._agent`` stayed set.

The half that fix exists for is quieter still. A SIGKILLed stdio server never
raises out of the agent's context at all, so the lifecycle task stayed parked on
its shutdown event forever and nothing ever failed. Every later tool call in the
case died with ``ClosedResourceError`` on the same reused client, and bench scored
those as the model's doing. On pydantic-ai 2.x the probe is a ``ping``: a dead
transport fails it locally with ``CONNECTION_CLOSED``; any answer from the server
— even a refusal — is proof of life.
"""

from __future__ import annotations

import asyncio

import pytest
from mcp.types import CONNECTION_CLOSED

from condor_compat.acp.pydantic_ai_client import (
    MCP_TRANSPORT_POLL_SECONDS,
    PydanticAIClient,
    _dead_transport,
)


class _RpcError(Exception):
    def __init__(self, code: int):
        super().__init__(f"rpc error {code}")
        self.code = code


class _Transport:
    def __init__(self, command: str):
        self.command = command


class _Client:
    def __init__(self, command: str, closed: bool, answers_with_error: bool):
        self.transport = _Transport(command)
        self.closed = closed
        self.answers_with_error = answers_with_error

    async def ping(self):
        if self.closed:
            raise _RpcError(CONNECTION_CLOSED)  # failed locally: nobody answered
        if self.answers_with_error:
            raise _RpcError(-32601)  # the server's own "method not found"
        return {}


class _Server:
    """An MCP toolset the way the lifecycle probe reads one."""

    def __init__(self, command: str = "uv", closed: bool = False, answers_with_error: bool = False):
        self.client = _Client(command, closed, answers_with_error)


class _FakeAgent:
    """The agent's own async context is what holds the MCP servers open."""

    def __init__(self, fail: BaseException | None = None):
        self._fail = fail
        self.entered = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc_info):
        if self._fail is not None:
            raise self._fail


def _client(agent: _FakeAgent, servers: list[_Server] | None = None) -> PydanticAIClient:
    client = PydanticAIClient(model="ollama:llama3.1")
    client._agent = agent
    client._ready_event = asyncio.Event()
    client._shutdown_event = asyncio.Event()
    client._mcp_servers = list(servers or [])
    return client


# ── the probe ─────────────────────────────────────────────────────────────────

def test_a_live_server_reads_as_healthy():
    assert asyncio.run(_dead_transport([_Server()])) is None


def test_a_server_that_refuses_the_ping_is_alive():
    """An MCP SDK 2.x server has no `ping` method and says so — that is an answer."""
    assert asyncio.run(_dead_transport([_Server(answers_with_error=True)])) is None


def test_a_server_that_never_opened_its_client_is_not_called_dead():
    """Absent is not closed — otherwise startup would look like a collapse."""
    server = _Server()
    del server.client
    assert asyncio.run(_dead_transport([server])) is None


def test_a_closed_transport_names_the_server():
    servers = [_Server(), _Server(command="uvx", closed=True)]
    assert asyncio.run(_dead_transport(servers)) == "uvx"


# ── the lifecycle task ────────────────────────────────────────────────────────

def test_a_killed_subprocess_tears_the_client_down():
    """The case this fix exists for: streams close, nothing raises, poll catches it."""

    async def _run():
        server = _Server()
        client = _client(_FakeAgent(), [server])
        task = asyncio.create_task(client._run_mcp_lifecycle())
        await client._ready_event.wait()
        assert client.alive

        server.client.closed = True  # SIGKILL, as the transport sees it
        await asyncio.wait_for(task, timeout=MCP_TRANSPORT_POLL_SECONDS * 4)

        assert not client.alive, "a toolless client must not report healthy"
        assert isinstance(client._lifecycle_error, ConnectionError)
        assert "uv" in str(client._lifecycle_error)

    asyncio.run(_run())


def test_a_clean_shutdown_is_not_mistaken_for_a_collapse():
    async def _run():
        client = _client(_FakeAgent(), [_Server()])
        task = asyncio.create_task(client._run_mcp_lifecycle())
        await client._ready_event.wait()
        client._shutdown_event.set()
        await asyncio.wait_for(task, timeout=MCP_TRANSPORT_POLL_SECONDS * 4)
        assert client._lifecycle_error is None

    asyncio.run(_run())


def test_a_post_startup_failure_is_recorded_rather_than_swallowed():
    async def _run():
        boom = RuntimeError("server exploded")
        client = _client(_FakeAgent(fail=boom), [_Server()])
        task = asyncio.create_task(client._run_mcp_lifecycle())
        await client._ready_event.wait()
        client._shutdown_event.set()
        await asyncio.wait_for(task, timeout=MCP_TRANSPORT_POLL_SECONDS * 4)
        assert client._lifecycle_error is boom
        assert not client.alive

    asyncio.run(_run())


def test_a_startup_failure_still_reaches_start():
    async def _run():
        boom = RuntimeError("never came up")
        client = _client(_FakeAgent(), [_Server()])
        client._ready_event.set()  # pretend ready, then fail: post-startup path
        client._agent = _FakeAgent(fail=boom)
        client._shutdown_event.set()
        await client._run_mcp_lifecycle()
        assert client._lifecycle_error is boom

    asyncio.run(_run())


def test_a_cancelled_lifecycle_does_not_complete_successfully():
    """`stop()` waits on this task; a swallowed CancelledError reports no problem."""

    async def _run():
        client = _client(_FakeAgent(), [_Server()])
        task = asyncio.create_task(client._run_mcp_lifecycle())
        await client._ready_event.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())


# ── the prompt path ───────────────────────────────────────────────────────────

def test_a_turn_that_meets_a_dead_transport_marks_the_client_dead_at_once():
    """The prompt usually hits it before the poll does; `alive` must not lag."""
    client = _client(_FakeAgent(), [_Server(closed=True)])
    exc = RuntimeError("ClosedResourceError")
    asyncio.run(client._mark_dead_if_transport_closed(exc))
    assert client._lifecycle_error is exc
    assert not client.alive


def test_a_turn_that_fails_for_other_reasons_leaves_the_client_alive():
    """A model erroring is not an infra collapse — the client stays reusable."""
    client = _client(_FakeAgent(), [_Server()])
    asyncio.run(client._mark_dead_if_transport_closed(RuntimeError("model said no")))
    assert client._lifecycle_error is None
    assert client.alive


# ── what a refused tool call looks like in the trace ──────────────────────────

def test_a_refused_tool_call_is_recorded_as_an_error():
    """pydantic-ai 2.x returns a tool that raised as a ToolReturnPart whose outcome
    is not "success". Recorded as a plain "completed" output, a refused create read
    as one that may have landed — and teardown deleted a routine that predated it."""
    from pydantic_ai import Agent, ModelRetry
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from bench.cleanup import tool_error
    from condor_compat.acp.client import ToolCallUpdate

    def script(messages, info):
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("manage_routines", {"name": "x"}, tool_call_id="c1")])
        return ModelResponse(parts=[TextPart("it already exists")])

    agent = Agent(FunctionModel(script), retries=2)

    @agent.tool_plain
    def manage_routines(name: str) -> str:
        raise ModelRetry(f"Routine '{name}' already exists.")

    client = PydanticAIClient(model="ollama:llama3.1")
    client._agent = agent

    async def collect():
        return [e async for e in client.prompt_stream("make routine x")]

    updates = [e for e in asyncio.run(collect()) if isinstance(e, ToolCallUpdate) and e.output]
    assert updates and updates[0].status == "failed"
    assert tool_error(updates[0].output) == "Routine 'x' already exists."
