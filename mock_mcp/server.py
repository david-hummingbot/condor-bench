#!/usr/bin/env python
"""Mock MCP server for condor-bench tick tests.

Launched as a stdio subprocess by PydanticAIClient during tick benchmarks.
Returns scenario-specific canned data for each tool and records all calls
to BENCH_TOOL_LOG for post-run grading.

Environment variables:
  BENCH_SCENARIO_FILE  JSON file with {"mock_tools": {tool_name: {"response": "..."}}}
  BENCH_TOOL_LOG       JSONL file to append recorded tool calls
"""

import json
import os
import sys
from pathlib import Path

_scenario_file = os.environ.get("BENCH_SCENARIO_FILE", "")
_tool_log = os.environ.get("BENCH_TOOL_LOG", "")
_mock_tools: dict = {}

if _scenario_file and Path(_scenario_file).exists():
    try:
        with open(_scenario_file) as f:
            _mock_tools = json.load(f).get("mock_tools", {})
    except Exception:
        pass


def _log(tool: str, args: dict, result: str) -> None:
    if not _tool_log:
        return
    try:
        with open(_tool_log, "a") as f:
            f.write(json.dumps({"tool": tool, "args": args, "result": result}) + "\n")
    except Exception:
        pass


def _tool_response(tool_name: str, fallback: dict) -> str:
    cfg = _mock_tools.get(tool_name, {})
    return cfg.get("response", json.dumps(fallback))


try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("mcp package is required: pip install mcp>=1.9", file=sys.stderr)
    sys.exit(1)

mcp = FastMCP("condor-mock")


@mcp.tool()
def get_market_data(market: str = "BTC-USDT", interval: str = "1h", limit: int = 20) -> str:
    """Get OHLCV data and technical indicators for a trading pair."""
    result = _tool_response("get_market_data", {
        "symbol": market,
        "mid_price": 67000.0,
        "24h_change_pct": 0.2,
        "volume_24h_usd": 500_000_000,
        "rsi_14": 50.0,
        "bb_upper": 67800.0,
        "bb_mid": 67000.0,
        "bb_lower": 66200.0,
        "atr_14": 350.0,
        "funding_rate": 0.0001,
    })
    _log("get_market_data", {"market": market, "interval": interval, "limit": limit}, result)
    return result


@mcp.tool()
def manage_executors(
    action: str = "list",
    executor_type: str = "",
    controller_id: str = "",
    executor_id: str = "",
    executor_config: dict = None,
) -> str:
    """Create, list, stop, or get config schema for trading executors."""
    fallback = {"status": "ok", "executors": [], "message": f"Action '{action}' completed (mock)."}
    result = _tool_response("manage_executors", fallback)
    _log("manage_executors", {
        "action": action,
        "executor_type": executor_type,
        "executor_id": executor_id,
    }, result)
    return result


@mcp.tool()
def trading_agent_journal_write(
    entry_type: str = "action",
    text: str = "",
    category: str = "",
) -> str:
    """Write an action or learning entry to the strategy journal."""
    result = json.dumps({"status": "ok", "message": "Journal entry recorded."})
    _log("trading_agent_journal_write", {
        "entry_type": entry_type,
        "text": text,
        "category": category,
    }, result)
    return result


@mcp.tool()
def send_notification(text: str = "") -> str:
    """Send a notification message to the user on Telegram."""
    result = json.dumps({"status": "ok", "message": "Notification sent."})
    _log("send_notification", {"text": text}, result)
    return result


@mcp.tool()
def manage_skill(action: str = "list", name: str = "", content: str = "") -> str:
    """Read or list trading playbooks (skills)."""
    result = json.dumps({"skills": [], "count": 0})
    _log("manage_skill", {"action": action, "name": name}, result)
    return result


@mcp.tool()
def manage_memory(
    action: str = "list",
    name: str = "",
    description: str = "",
    content: str = "",
    type: str = "",
) -> str:
    """Read or write persistent memory about the user or environment."""
    result = json.dumps({"memories": [], "count": 0})
    _log("manage_memory", {"action": action, "name": name}, result)
    return result


@mcp.tool()
def manage_routines(
    action: str = "list",
    name: str = "",
    config: dict = None,
    strategy_id: str = "",
) -> str:
    """Discover or run analysis routines."""
    result = json.dumps({"routines": [], "count": 0})
    _log("manage_routines", {"action": action, "name": name}, result)
    return result


@mcp.tool()
def search_history(query: str = "", limit: int = 10) -> str:
    """Search historical trade records and executor snapshots."""
    result = json.dumps({"trades": [], "total": 0, "message": "No history in mock mode."})
    _log("search_history", {"query": query, "limit": limit}, result)
    return result


@mcp.tool()
def explore_geckoterminal(query: str = "") -> str:
    """Search DeFi pools and liquidity data via GeckoTerminal."""
    result = json.dumps({"pools": [], "message": "No pools in mock mode."})
    _log("explore_geckoterminal", {"query": query}, result)
    return result


if __name__ == "__main__":
    mcp.run()
