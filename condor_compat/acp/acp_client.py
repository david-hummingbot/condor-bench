"""ACPClient — vendored from condor/condor/acp/client.py.

Changes from production:
- Permission requests are auto-approved (no Telegram confirmation).
- reap_stale_acp_trees() and _ps_rows() retained for completeness but not called by bench.
- ACP_COMMANDS and resolve_acp() included for model-key routing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import time
from typing import Any, AsyncIterator

from .client import (
    ACPEvent, PermissionCallback, PromptDone, TextChunk, ThoughtChunk,
    ToolCallEvent, ToolCallUpdate, Heartbeat,
)
from .jsonrpc import JSONRPCPeer

log = logging.getLogger(__name__)

ACP_COMMANDS: dict[str, str] = {
    "claude-code": "claude-agent-acp",
    "claude-acp": "claude-agent-acp",
    "gemini": "npx @google/gemini-cli --acp",
    "copilot": "npx @github/copilot --acp --stdio",
    "codex": "npx @zed-industries/codex-acp",
}
_CLAUDE_ACP_BASES = {"claude-code", "claude-acp"}


def resolve_acp(agent_key: str) -> tuple[str, dict[str, str]]:
    base, _, model = agent_key.partition(":")
    command = ACP_COMMANDS.get(base, ACP_COMMANDS["claude-code"])
    env: dict[str, str] = {}
    if model and base in _CLAUDE_ACP_BASES:
        env["ANTHROPIC_MODEL"] = model
    return command, env


def is_acp_model(model_key: str) -> bool:
    base = model_key.partition(":")[0]
    return base in ACP_COMMANDS


def _descendant_pids(root: int) -> set[int]:
    try:
        out = subprocess.run(["ps", "-eo", "pid=,ppid="], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return set()
    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    found: set[int] = set()
    stack = [root]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _signal_all(pids: set[int], pgid: int | None, sig: int) -> None:
    if pgid is not None:
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


class ACPClient:
    """Manages the lifecycle of an ACP subprocess agent (bench version: auto-approves permissions)."""

    def __init__(
        self,
        command: str,
        working_dir: str | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
        permission_callback: PermissionCallback | None = None,
        extra_env: dict[str, str] | None = None,
    ):
        self.command = command
        self.working_dir = working_dir or os.getcwd()
        self.mcp_servers: list[dict[str, Any]] = mcp_servers or []
        self.permission_callback = permission_callback
        self.extra_env = extra_env
        self._process: asyncio.subprocess.Process | None = None
        self._peer = JSONRPCPeer()
        self._session_id: str | None = None
        self._read_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._event_queue: asyncio.Queue[ACPEvent | None] = asyncio.Queue()
        self._current_req_id: int | None = None
        self._peer.register_handler("session/update", self._on_session_update)
        self._peer.register_handler("session/request_permission", self._on_request_permission)

    async def start(self) -> None:
        env = dict(os.environ)
        if self.extra_env:
            env.update(self.extra_env)
        self._process = await asyncio.create_subprocess_shell(
            self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.working_dir,
            env=env,
            limit=10 * 1024 * 1024,
            start_new_session=True,
        )
        self._read_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            await self._peer.send_request(
                "initialize",
                {"protocolVersion": 1, "clientCapabilities": {}, "clientInfo": {"name": "condor-bench", "version": "0.1.0"}},
                self._process.stdin,
            )
            result = await self._peer.send_request(
                "session/new",
                {"cwd": self.working_dir, "mcpServers": self.mcp_servers},
                self._process.stdin,
            )
        except Exception:
            await self.stop()
            raise
        self._session_id = result["sessionId"]

    async def stop(self) -> None:
        self._peer.cancel_all()
        for task in (self._read_task, self._stderr_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._process and self._process.returncode is None:
            pid = self._process.pid
            pids = await asyncio.to_thread(_descendant_pids, pid)
            pids.add(pid)
            try:
                pgid = os.getpgid(pid)
            except (ProcessLookupError, PermissionError):
                pgid = None
            _signal_all(pids, pgid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                survivors = {p for p in pids if _alive(p)}
                if survivors:
                    _signal_all(survivors, pgid, signal.SIGKILL)
        self._process = None

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def _read_loop(self) -> None:
        assert self._process and self._process.stdout
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                await self._peer.handle_line(line.decode(), self._process.stdin)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("ACP read loop error")
        self._peer.cancel_all()
        self._event_queue.put_nowait(PromptDone(stop_reason="disconnected"))

    async def _drain_stderr(self) -> None:
        assert self._process and self._process.stderr
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                text = line.decode(errors="replace").rstrip()
                if text:
                    log.debug("ACP stderr: %s", text)
        except asyncio.CancelledError:
            return

    async def prompt(self, text: str) -> str:
        chunks: list[str] = []
        async for event in self.prompt_stream(text):
            if isinstance(event, TextChunk):
                chunks.append(event.text)
        return "".join(chunks)

    async def prompt_stream(self, text: str) -> AsyncIterator[ACPEvent]:
        assert self._process and self._session_id
        if self._current_req_id is not None:
            old = self._peer._pending.pop(self._current_req_id, None)
            if old and not old.done():
                old.cancel()
            self._current_req_id = None
        while not self._event_queue.empty():
            self._event_queue.get_nowait()

        req_id = self._peer._next_id
        self._peer._next_id += 1
        self._current_req_id = req_id
        msg = {
            "jsonrpc": "2.0",
            "method": "session/prompt",
            "params": {"sessionId": self._session_id, "prompt": [{"type": "text", "text": text}]},
            "id": req_id,
        }
        self._process.stdin.write((json.dumps(msg) + "\n").encode())
        await self._process.stdin.drain()

        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._peer._pending[req_id] = future

        def _on_response(fut: asyncio.Future) -> None:
            if self._current_req_id != req_id:
                return
            if fut.cancelled():
                self._event_queue.put_nowait(PromptDone(stop_reason="cancelled"))
            elif fut.exception():
                self._event_queue.put_nowait(PromptDone(stop_reason="error"))
            else:
                result = fut.result()
                reason = result.get("stopReason", "end_turn") if isinstance(result, dict) else "end_turn"
                self._event_queue.put_nowait(PromptDone(stop_reason=reason))

        future.add_done_callback(_on_response)
        loop = asyncio.get_event_loop()
        start_time = loop.time()

        while True:
            try:
                event = await asyncio.wait_for(self._event_queue.get(), timeout=30)
            except asyncio.TimeoutError:
                elapsed = loop.time() - start_time
                if not self.alive:
                    yield PromptDone(stop_reason="disconnected")
                    break
                if elapsed > 1860:
                    yield PromptDone(stop_reason="timeout")
                    break
                yield Heartbeat(elapsed_seconds=elapsed)
                continue
            if event is None:
                break
            yield event
            if isinstance(event, PromptDone):
                break
        if self._current_req_id == req_id:
            self._current_req_id = None

    def _on_session_update(self, sessionId: str = "", update: dict[str, Any] | None = None, _meta: dict | None = None, **kw: Any) -> None:
        update = update or {}
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            text = update.get("content", {}).get("text", "")
            if text:
                self._event_queue.put_nowait(TextChunk(text=text))
        elif kind == "agent_thought_chunk":
            text = update.get("content", {}).get("text", "")
            if text:
                self._event_queue.put_nowait(ThoughtChunk(text=text))
        elif kind == "tool_call":
            self._event_queue.put_nowait(ToolCallEvent(
                tool_call_id=update.get("toolCallId", ""),
                title=update.get("title", ""),
                status=update.get("status", "pending"),
                kind=update.get("kind", "other"),
                input=update.get("input"),
            ))
        elif kind == "tool_call_update":
            self._event_queue.put_nowait(ToolCallUpdate(
                tool_call_id=update.get("toolCallId", ""),
                status=update.get("status"),
                title=update.get("title"),
                output=update.get("output"),
            ))

    async def _on_request_permission(self, sessionId: str = "", options: list[dict[str, Any]] | None = None, toolCall: dict[str, Any] | None = None, _meta: dict | None = None, **kw: Any) -> dict[str, Any]:
        options = options or []
        if self.permission_callback:
            return await self.permission_callback(toolCall or {}, options)
        # Bench default: auto-approve
        for opt in options:
            if opt.get("kind") in ("allow_once", "allow_always"):
                return {"outcome": {"outcome": "selected", "optionId": opt["optionId"]}}
        if options:
            return {"outcome": {"outcome": "selected", "optionId": options[0]["optionId"]}}
        return {"outcome": {"outcome": "cancelled"}}
