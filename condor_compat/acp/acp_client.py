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
from collections import deque
from typing import Any, AsyncIterator

from .client import (
    ACPEvent, PermissionCallback, PromptDone, TextChunk, ThoughtChunk,
    ToolCallEvent, ToolCallUpdate, Heartbeat, UsageEvent,
    parse_meta_usage, parse_prompt_usage,
    # Re-exported: the wire shape is shared with the PydanticAI path, and tests
    # and callers import it from here.
    unwrap_content_blocks,
)
from .jsonrpc import JSONRPCPeer

log = logging.getLogger(__name__)

# How much of the bridge's stderr to keep for error reporting. Enough to hold a
# pretty-printed JSON-RPC error (the API 400 that broke every prompt in a run spans
# about a dozen lines) without retaining a whole session's chatter.
STDERR_TAIL_LINES = 40

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


# ── ACP wire translation ───────────────────────────────────────────────────────
# The ACP wire does not spell a tool call the way the rest of bench reads one. A
# `tool_call` frame from claude-agent-acp 0.28 looks like:
#
#   {"sessionUpdate": "tool_call", "toolCallId": "toolu_…", "title": "ToolSearch",
#    "kind": "other", "status": "pending", "rawInput": {...}, "content": [],
#    "_meta": {"claudeCode": {"toolName": "mcp__mcp-hummingbot__get_market_data"}}}
#
# and the completing update carries `rawOutput` plus a `content` block list.
# Reading `input`/`output` — which is what this vendored copy did — yields None on
# every frame, and the damage is silent rather than loud:
#
#   * every tool call recorded with args {} → tool_params scored 0.0 on any case
#     that pins parameters,
#   * no tool responses at all → live_validity None (its weight quietly moved to
#     answer quality), and the judge saw a transcript whose figures had no tool
#     output behind them, which it is instructed to treat as fabrication. A
#     correct, tool-grounded answer scored 0.05.
#
# condor fixed the input half upstream in `normalize_tool_call` (SEC-093) for the
# same reason on the danger-gate side; the output half it does not need, because
# condor only renders tool output while bench scores it.
def acp_tool_input(payload: dict[str, Any]) -> Any:
    """A tool call's arguments. ``rawInput`` on the wire, ``input`` in older frames.

    Passed through as it arrives, without coercing a missing value to ``{}``:
    "no arguments I can read" and "an empty argument set" are different facts, and
    the param metric should be able to tell them apart.
    """
    args = payload.get("rawInput")
    if args is None:
        args = payload.get("input")
    return args


def acp_tool_name(payload: dict[str, Any]) -> str:
    """The tool's real name, not its display title.

    ``_meta.claudeCode.toolName`` is authoritative and fully qualified
    (``mcp__mcp-hummingbot__get_market_data``, which
    ``metrics.tool_accuracy.normalize_tool_name`` then reduces to the bare tool).
    ``title`` is a human-facing label that happens to match for MCP tools and does
    not for built-ins, so scoring on it would depend on the bridge's rendering.
    """
    meta = payload.get("_meta") or {}
    vendor = meta.get("claudeCode") or {}
    name = vendor.get("toolName") or payload.get("title") or ""
    return str(name)


def acp_tool_output(payload: dict[str, Any]) -> Any:
    """A tool result, from whichever field this bridge used.

    ``rawOutput`` is the structured result; ``content`` is the rendered block list
    that accompanies it. Both appear on the completing ``tool_call_update``, and a
    frame that carries neither (a mid-call status tick) yields None so the caller
    skips it instead of recording an empty response.

    Content-block envelopes are unwrapped here, at the one seam every consumer
    reads through — see :func:`unwrap_content_blocks`.
    """
    for key in ("rawOutput", "output"):
        if payload.get(key) is not None:
            return unwrap_content_blocks(payload[key]) or payload[key]
    blocks = payload.get("content")
    unwrapped = unwrap_content_blocks(blocks)
    if unwrapped:
        return unwrapped
    # Some bridges only report the structured response under the vendor extension.
    vendor = (payload.get("_meta") or {}).get("claudeCode") or {}
    if vendor.get("toolResponse") is not None:
        return vendor["toolResponse"]
    return None


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
        # Populated by start() from the session/new reply.
        self.session_models: dict[str, Any] = {}
        self._read_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._event_queue: asyncio.Queue[ACPEvent | None] = asyncio.Queue()
        self._current_req_id: int | None = None
        # Last lines the bridge wrote to stderr. Kept because that is where an ACP
        # agent explains itself, and logging it at DEBUG meant a 400 that killed
        # every prompt in a run left no trace anywhere the operator would look.
        self._stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
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
        # The bridge advertises the models it will accept here. Kept so a caller can
        # offer them instead of guessing at ids — an unusable model id is not a
        # cosmetic mistake: claude-agent-acp fails every prompt in the run with a
        # 400 when the configured model rejects the thinking parameter it sends.
        models = result.get("models")
        self.session_models = models if isinstance(models, dict) else {}

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
                    self._stderr_tail.append(text)
                    log.debug("ACP stderr: %s", text)
        except asyncio.CancelledError:
            return

    def stderr_tail(self, limit: int = STDERR_TAIL_LINES) -> str:
        """The bridge's most recent stderr, for attaching to a failure."""
        lines = list(self._stderr_tail)[-limit:]
        return "\n".join(lines)

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
                # Carry the bridge's message. A `session/prompt` that fails — an API
                # 400 for an unsupported parameter, say — otherwise reaches the
                # scorer as an empty response with no error, which reads as "the
                # model said nothing" instead of "the request never ran".
                exc = fut.exception()
                detail = str(exc) or exc.__class__.__name__
                tail = self.stderr_tail()
                if tail:
                    detail = f"{detail}\n--- ACP stderr ---\n{tail}"
                self._event_queue.put_nowait(
                    PromptDone(stop_reason="error", error=detail)
                )
            else:
                result = fut.result()
                reason = result.get("stopReason", "end_turn") if isinstance(result, dict) else "end_turn"
                if isinstance(result, dict):
                    # Two dialects: claude-agent-acp answers with a `usage`
                    # object, the Gemini CLI reports via the `_meta` vendor
                    # extension. Try both so a bridge speaking either one is
                    # measured rather than looking free.
                    usage = parse_prompt_usage(result.get("usage")) or parse_meta_usage(
                        result.get("_meta")
                    )
                    if usage is not None:
                        self._event_queue.put_nowait(usage)
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
                title=acp_tool_name(update),
                status=update.get("status", "pending"),
                kind=update.get("kind", "other"),
                input=acp_tool_input(update),
            ))
        elif kind == "tool_call_update":
            self._event_queue.put_nowait(ToolCallUpdate(
                tool_call_id=update.get("toolCallId", ""),
                status=update.get("status"),
                title=update.get("title") or None,
                output=acp_tool_output(update),
                input=acp_tool_input(update),
            ))
        elif kind == "usage_update":
            # claude-agent-acp emits this once per assistant result with the
            # context-window occupancy and the session's cumulative USD cost. The
            # token breakdown arrives separately on the session/prompt response
            # (see _on_response); both fold into one usage row.
            cost = update.get("cost") or {}
            self._event_queue.put_nowait(UsageEvent(
                context_used=update.get("used"),
                context_size=update.get("size"),
                cost_usd=(
                    cost.get("amount")
                    if str(cost.get("currency", "USD")).upper() == "USD"
                    else None
                ),
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
