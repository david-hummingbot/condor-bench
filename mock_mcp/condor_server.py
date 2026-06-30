#!/usr/bin/env python3
"""Mock condor MCP server for benchmarking.

Exposes the same tools as the production condor MCP server.
Reads canned responses from BENCH_SCENARIO_FILE; logs all calls to BENCH_TOOL_LOG.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("condor")


def _load() -> dict:
    f = os.environ.get("BENCH_SCENARIO_FILE", "")
    if f and Path(f).exists():
        return json.loads(Path(f).read_text())
    return {}


def _log(tool: str, args: dict, result: dict) -> None:
    log_path = os.environ.get("BENCH_TOOL_LOG", "")
    if not log_path:
        return
    with open(log_path, "a") as fh:
        fh.write(json.dumps({"tool": tool, "args": args, "result": result}) + "\n")


def _mock(tool: str, default: dict) -> dict:
    return _load().get("mock_tools", {}).get(tool, default)


@mcp.tool()
async def trading_agent_journal_write(
    entry_type: str = "action",
    text: str = "",
    category: str | None = None,
    agent_id: str | None = None,
) -> dict:
    args = {"entry_type": entry_type, "text": text}
    result = {"status": "written", "entry_id": "mock-journal-001"}
    _log("trading_agent_journal_write", args, result)
    return result


@mcp.tool()
async def trading_agent_journal_read(
    agent_id: str | None = None,
    limit: int = 20,
    entry_type: str | None = None,
) -> dict:
    args = {"agent_id": agent_id, "limit": limit}
    result = _mock("trading_agent_journal_read", {"entries": [], "total": 0})
    _log("trading_agent_journal_read", args, result)
    return result


@mcp.tool()
async def send_notification(text: str = "", chat_id: int | None = None) -> dict:
    args = {"text": text}
    result = {"status": "sent", "message_id": "mock-msg-001"}
    _log("send_notification", args, result)
    return result


@mcp.tool()
async def manage_memory(
    action: str = "list",
    name: str | None = None,
    description: str | None = None,
    content: str | None = None,
    type: str | None = None,
) -> dict:
    args = {"action": action, "name": name}
    if action == "list":
        result = _mock("manage_memory_list", {"memories": []})
    elif action == "read":
        result = _mock("manage_memory_read", {"name": name, "content": "Mock memory content."})
    elif action == "write":
        result = {"status": "saved", "name": name}
    else:
        result = {"status": "ok"}
    _log("manage_memory", args, result)
    return result


@mcp.tool()
async def manage_skill(
    action: str = "list",
    name: str | None = None,
    description: str | None = None,
    when_to_use: str | None = None,
    body: str | None = None,
) -> dict:
    args = {"action": action, "name": name}
    if action == "list":
        result = _mock("manage_skill_list", {"skills": []})
    elif action == "read":
        result = _mock("manage_skill_read", {"name": name, "body": "No skill body available."})
    else:
        result = {"status": "ok"}
    _log("manage_skill", args, result)
    return result


@mcp.tool()
async def manage_routines(
    action: str = "list",
    name: str | None = None,
    config: dict | None = None,
) -> dict:
    args = {"action": action, "name": name}
    if action == "list":
        result = _mock("manage_routines_list", {"routines": []})
    elif action == "run":
        result = _mock("manage_routines_run", {"status": "completed", "output": "Routine ran successfully."})
    else:
        result = {"status": "ok"}
    _log("manage_routines", args, result)
    return result


@mcp.tool()
async def manage_servers(
    action: str = "list",
    name: str | None = None,
    host: str | None = None,
    port: int | None = None,
    username: str | None = None,
    password: str | None = None,
) -> dict:
    args = {"action": action, "name": name}
    if action == "list":
        result = _mock("manage_servers_list", {"servers": []})
    elif action == "add":
        result = {"status": "added", "name": name, "message": f"Server '{name}' added successfully."}
    elif action == "test":
        result = _mock("manage_servers_test", {"status": "connected", "latency_ms": 45})
    else:
        result = {"status": "ok"}
    _log("manage_servers", args, result)
    return result


@mcp.tool()
async def get_user_context() -> dict:
    result = _mock("get_user_context", {
        "preferences": {},
        "profile": {"timezone": "UTC", "currency": "USD"},
    })
    _log("get_user_context", {}, result)
    return result


@mcp.tool()
async def consult(agent: str = "", task: str = "", context: str = "") -> dict:
    args = {"agent": agent, "task": task}
    result = _mock("consult", {"answer": f"[Mock answer from {agent} agent for: {task[:50]}]"})
    _log("consult", args, result)
    return result


@mcp.tool()
async def manage_trading_agent(
    action: str = "list",
    agent_id: str | None = None,
    config: dict | None = None,
) -> dict:
    args = {"action": action, "agent_id": agent_id}
    if action == "list":
        result = _mock("manage_trading_agent_list", {"agents": []})
    elif action == "start":
        result = {"status": "started", "agent_id": agent_id}
    elif action == "stop":
        result = {"status": "stopped", "agent_id": agent_id}
    else:
        result = {"status": "ok"}
    _log("manage_trading_agent", args, result)
    return result


if __name__ == "__main__":
    _load()
    mcp.run(transport="stdio")
