#!/usr/bin/env python3
"""Mock mcp-hummingbot MCP server for benchmarking.

Exposes the same tools as the production hummingbot_api MCP server.
Reads canned responses from BENCH_SCENARIO_FILE; logs all calls to BENCH_TOOL_LOG.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mcp-hummingbot")

_scenario: dict = {}
_tool_log: list[str] = []


def _load() -> None:
    global _scenario
    f = os.environ.get("BENCH_SCENARIO_FILE", "")
    if f and Path(f).exists():
        _scenario = json.loads(Path(f).read_text())


def _log(tool: str, args: dict, result: dict) -> None:
    log_path = os.environ.get("BENCH_TOOL_LOG", "")
    if not log_path:
        return
    record = {"tool": tool, "args": args, "result": result}
    with open(log_path, "a") as fh:
        fh.write(json.dumps(record) + "\n")


def _mock(tool: str, default: dict) -> dict:
    return _scenario.get("mock_tools", {}).get(tool, default)


@mcp.tool()
async def get_market_data(
    connector_name: str = "binance",
    data_type: str = "prices",
    trading_pair: str = "BTC-USDT",
    trading_pairs: list[str] | None = None,
    interval: str = "1m",
    days: int = 1,
    is_buy: bool | None = None,
    query_type: str | None = None,
    query_value: float | None = None,
) -> dict:
    args = {"connector_name": connector_name, "data_type": data_type, "trading_pair": trading_pair}
    result = _mock("get_market_data", {
        "mid_price": 65000.0,
        "best_bid": 64990.0,
        "best_ask": 65010.0,
        "24h_change_pct": 1.2,
        "24h_volume": 1250000000,
        "candles": [{"open": 64800, "high": 65200, "low": 64700, "close": 65000, "volume": 500}],
    })
    _log("get_market_data", args, result)
    return result


@mcp.tool()
async def manage_executors(
    action: str = "get_all_bots",
    executor_type: str | None = None,
    executor_id: str | None = None,
    executor_config: dict | None = None,
    controller_id: str | None = None,
    bot_name: str | None = None,
) -> dict:
    args = {"action": action, "executor_type": executor_type, "executor_id": executor_id}
    if action == "create":
        result = _mock("manage_executors_create", {"status": "created", "executor_id": "mock-exec-001"})
    elif action == "stop":
        result = _mock("manage_executors_stop", {"status": "stopped"})
    elif action == "performance_report":
        result = _mock("manage_executors_report", {
            "active_executors": [], "closed_executors": [],
            "total_pnl_quote": 0.0, "realized_pnl_quote": 0.0,
        })
    else:
        result = _mock("manage_executors", {
            "executors": [],
            "message": f"Action {action} completed",
        })
    _log("manage_executors", args, result)
    return result


@mcp.tool()
async def get_portfolio_overview(connector_name: str | None = None) -> dict:
    args = {"connector_name": connector_name}
    result = _mock("get_portfolio_overview", {
        "total_balance_usd": 10000.0,
        "available_balance_usd": 7500.0,
        "positions": [],
        "open_orders": [],
    })
    _log("get_portfolio_overview", args, result)
    return result


@mcp.tool()
async def search_history(
    data_type: str = "orders",
    account_names: list[str] | None = None,
    connector_names: list[str] | None = None,
    trading_pairs: list[str] | None = None,
    start_time: int | None = None,
    end_time: int | None = None,
    status: str | None = None,
    network: str | None = None,
    wallet_address: str | None = None,
    position_addresses: list[str] | None = None,
    limit: int = 10,
    offset: int = 0,
) -> dict:
    args = {"data_type": data_type, "limit": limit}
    result = _mock("search_history", {"results": [], "total": 0})
    _log("search_history", args, result)
    return result


@mcp.tool()
async def explore_geckoterminal(
    action: str = "search",
    query: str = "",
    network: str | None = None,
    pool_address: str | None = None,
) -> dict:
    args = {"action": action, "query": query}
    result = _mock("explore_geckoterminal", {"pools": [], "tokens": []})
    _log("explore_geckoterminal", args, result)
    return result


@mcp.tool()
async def explore_dex_pools(
    action: str = "search",
    query: str = "",
    network: str | None = None,
) -> dict:
    args = {"action": action, "query": query}
    result = _mock("explore_dex_pools", {"pools": []})
    _log("explore_dex_pools", args, result)
    return result


@mcp.tool()
async def manage_bots(action: str = "list", bot_name: str | None = None) -> dict:
    args = {"action": action, "bot_name": bot_name}
    result = _mock("manage_bots", {"bots": [], "status": "ok"})
    _log("manage_bots", args, result)
    return result


@mcp.tool()
async def manage_controllers(
    action: str = "list",
    bot_name: str | None = None,
    controller_id: str | None = None,
    config: dict | None = None,
) -> dict:
    args = {"action": action}
    result = _mock("manage_controllers", {"controllers": [], "status": "ok"})
    _log("manage_controllers", args, result)
    return result


@mcp.tool()
async def set_account_position_mode_and_leverage(
    account_name: str = "master_account",
    connector_name: str = "binance_perpetual",
    trading_pair: str | None = None,
    position_mode: str = "ONEWAY",
    leverage: int = 1,
) -> dict:
    args = {
        "account_name": account_name,
        "connector_name": connector_name,
        "trading_pair": trading_pair,
        "leverage": leverage,
    }
    result = _mock("set_account_position_mode_and_leverage", {"status": "ok"})
    _log("set_account_position_mode_and_leverage", args, result)
    return result


@mcp.tool()
async def configure_server(
    name: str | None = None,
    host: str | None = None,
    port: int | None = None,
    username: str | None = None,
    password: str | None = None,
) -> dict:
    args = {"name": name, "host": host, "port": port}
    result = _mock("configure_server", {
        "name": name or "mock", "host": host or "localhost", "port": port or 8000,
        "status": "connected",
    })
    _log("configure_server", args, result)
    return result


if __name__ == "__main__":
    _load()
    mcp.run(transport="stdio")
