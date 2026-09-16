"""A post-startup MCP collapse must be loud and must kill the client (CORR-332).

Ported from condor 39cb7d50. bench's copy of ``_run_mcp_lifecycle`` was a step
behind condor's even before that fix: its handler recorded an exception only when
one fired *before* the ready event, so a post-startup failure was discarded with
nothing logged and ``self._agent`` stayed set.

The half that fix exists for is quieter still. A SIGKILLed stdio server never
raises out of ``run_mcp_servers()`` at all — it only closes the MCP session's
streams — so the lifecycle task stayed parked on its shutdown event forever and
nothing ever failed. Every later tool call in the case died with
``ClosedResourceError`` on the same reused client, and bench scored those as the
model's doing.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from condor_compat.acp.pydantic_ai_client import (
    MCP_TRANSPORT_POLL_SECONDS,
    PydanticAIClient,
    _dead_transport,
)


class _Stream:
    def __init__(self, closed: bool = False):
        self._closed = closed


class _Server:
    """An MCP server the way the lifecycle probe reads one."""

    def __init__(self, command: str = "uv", closed: bool = False):
        self.command = command
        self._read_stream = _Stream(closed)
        self._write_stream = _Stream(closed)


class _FakeAgent:
    def __init__(self, fail: BaseException | None = None):
        self._fail = fail
        self.entered = False

    def run_mcp_servers(self):
        @contextlib.asynccontextmanager
        async def _ctx():
            self.entered = True
            yield
            if self._fail is not None:
                raise self._fail

        return _ctx()


def _client(agent: _FakeAgent, servers: list[_Server] | None = None) -> PydanticAIClient:
    client = PydanticAIClient(model="ollama:llama3.1")
    client._agent = agent
    client._ready_event = asyncio.Event()
    client._shutdown_event = asyncio.Event()
    client._mcp_servers = list(servers or [])
    return client


# ── the probe ─────────────────────────────────────────────────────────────────

def test_a_live_server_reads_as_healthy():
    assert _dead_transport([_Server()]) is None


def test_a_server_that_never_opened_its_streams_is_not_called_dead():
    """Absent is not closed — otherwise startup would look like a collapse."""
    server = _Server()
    del server._read_stream
    del server._write_stream
    assert _dead_transport([server]) is None


def test_a_closed_stream_names_the_server():
    assert _dead_transport([_Server(), _Server(command="uvx", closed=True)]) == "uvx"


# ── the lifecycle task ────────────────────────────────────────────────────────

def test_a_killed_subprocess_tears_the_client_down():
    """The case this fix exists for: streams close, nothing raises, poll catches it."""

    async def _run():
        server = _Server()
        client = _client(_FakeAgent(), [server])
        task = asyncio.create_task(client._run_mcp_lifecycle())
        await client._ready_event.wait()
        assert client.alive

        server._read_stream._closed = True  # SIGKILL, as the transport sees it
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
    client._mark_dead_if_transport_closed(exc)
    assert client._lifecycle_error is exc
    assert not client.alive


def test_a_turn_that_fails_for_other_reasons_leaves_the_client_alive():
    """A model erroring is not an infra collapse — the client stays reusable."""
    client = _client(_FakeAgent(), [_Server()])
    client._mark_dead_if_transport_closed(RuntimeError("model said no"))
    assert client._lifecycle_error is None
    assert client.alive
