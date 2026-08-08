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
    agent_id: str | None = None,
    entry_type: str = "action",
    text: str = "",
    reasoning: str = "",
    risk_note: str = "",
    tick: int = 0,
    category: str | None = None,
    section: str = "",
) -> dict:
    args = {"entry_type": entry_type, "text": text}
    result = {"status": "written", "entry_id": "mock-journal-001"}
    _log("trading_agent_journal_write", args, result)
    return result


@mcp.tool()
async def trading_agent_journal_read(
    agent_id: str | None = None,
    max_entries: int = 20,
    section: str | None = None,
) -> dict:
    args = {"agent_id": agent_id, "max_entries": max_entries}
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
    max_entries: int | None = None,
    query: str | None = None,
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
    agent: str | None = None,
    content: str | None = None,
    file: str | None = None,
    max_entries: int | None = None,
    query: str | None = None,
    references_routine: str | None = None,
    shared: bool | None = None,
    strategy_id: str | None = None,
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
    agent: str | None = None,
    code: str | None = None,
    shared: bool | None = None,
    strategy_id: str | None = None,
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
) -> dict:
    args = {"action": action, "name": name}
    if action == "list":
        result = _mock("manage_servers_list", {"servers": []})
    elif action == "status":
        result = _mock("manage_servers_status", {
            "server": name or "default", "status": "online", "message": "Connected",
        })
    elif action == "test":
        # Not a documented production action, but c013 in the dataset exercises
        # it — kept so that case's mock_tools key stays live.
        result = _mock("manage_servers_test", {"status": "connected", "latency_ms": 45})
    else:
        result = {"status": "ok"}
    _log("manage_servers", args, result)
    return result


@mcp.tool()
async def get_user_context() -> dict:
    result = _mock("get_user_context", {
        "active_server": "default",
        "user_role": "user",
        "is_admin": False,
        "active_agent_key": "claude-acp:sonnet",
        "custom_llm_endpoints": [],
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
    if action in ("list", "list_agents", "list_agent_definitions"):
        result = _mock("manage_trading_agent_list", {"agents": []})
    elif action == "start":
        result = {"status": "started", "agent_id": agent_id}
    elif action == "stop":
        result = {"status": "stopped", "agent_id": agent_id}
    else:
        result = {"status": "ok"}
    _log("manage_trading_agent", args, result)
    return result


@mcp.tool()
async def delegate(
    action: str = "start",
    agent: str | None = None,
    task: str | None = None,
    task_id: str | None = None,
    on_complete: str = "notify",
) -> dict:
    args = {"action": action, "agent": agent, "task_id": task_id}
    if action == "start":
        result = _mock("delegate_start", {"task_id": "mock-task-001", "status": "started"})
    elif action == "get":
        result = _mock("delegate_get", {
            "task_id": task_id, "status": "completed",
            "result": "[Mock delegated result]",
        })
    else:
        result = _mock("delegate", {"status": "ok"})
    _log("delegate", args, result)
    return result


@mcp.tool()
async def get_available_models(
    openrouter_query: str | None = None,
    openrouter_limit: int | None = None,
) -> dict:
    args = {"openrouter_query": openrouter_query, "openrouter_limit": openrouter_limit}
    result = _mock("get_available_models", {"models": [], "recommended": None})
    _log("get_available_models", args, result)
    return result


@mcp.tool()
async def manage_notes(
    action: str = "list",
    key: str | None = None,
    value: str | None = None,
) -> dict:
    args = {"action": action, "key": key}
    if action == "list":
        result = _mock("manage_notes_list", {"notes": []})
    elif action == "get":
        result = _mock("manage_notes_get", {"key": key, "value": None})
    else:
        result = _mock("manage_notes", {"status": "ok"})
    _log("manage_notes", args, result)
    return result


if __name__ == "__main__":
    _load()
    mcp.run(transport="stdio")
