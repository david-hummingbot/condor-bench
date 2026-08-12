"""Market warmup: extract pins from cases and wait for trading rules."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from bench import market_warmup
from bench.market_warmup import (
    MarketRef,
    WarmupReport,
    ensure_markets_for_case,
    markets_from_case,
    warmup_failure_card,
)


@pytest.fixture
def stub(monkeypatch):
    """Route market_warmup's AsyncClient at a handler."""
    seen: list[tuple[str, str, bytes | None]] = []

    def install(handler):
        real = httpx.AsyncClient

        def wrapped(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.path, request.content or None))
            return handler(request)

        def factory(**kwargs):
            return real(transport=httpx.MockTransport(wrapped), **kwargs)

        monkeypatch.setattr(market_warmup.httpx, "AsyncClient", factory)
        return seen

    return install


def _json(payload, status=200):
    return httpx.Response(status, json=payload)


def test_markets_from_expected_tool_params():
    case = SimpleNamespace(
        expected_tool_params={
            "manage_executors": {
                "action": "create",
                "trading_pair": "RLUSD-XRP",
                "connector_name": "xrpl",
            }
        },
        config={},
    )
    assert markets_from_case(case) == [MarketRef("xrpl", "RLUSD-XRP")]


def test_markets_from_trading_pairs_list_and_dedupe():
    case = SimpleNamespace(
        expected_tool_params={
            "get_market_data": {
                "connector_name": "binance",
                "trading_pairs": ["BTC-USDT", "ETH-USDT"],
            },
            "manage_executors": {
                "connector_name": "binance",
                "trading_pair": "BTC-USDT",
            },
        },
        config={},
    )
    assert markets_from_case(case) == [
        MarketRef("binance", "BTC-USDT"),
        MarketRef("binance", "ETH-USDT"),
    ]


def test_markets_from_tick_config_connector_alias():
    case = SimpleNamespace(
        expected_tool_params={},
        config={"connector": "xrpl", "trading_pair": "RLUSD-XRP"},
    )
    assert markets_from_case(case) == [MarketRef("xrpl", "RLUSD-XRP")]


def test_markets_empty_when_no_pins():
    case = SimpleNamespace(expected_tool_params={}, config={})
    assert markets_from_case(case) == []


async def test_ensure_noop_without_markets(monkeypatch):
    monkeypatch.setattr(
        market_warmup,
        "staging_config",
        lambda: {
            "api_url": "http://staging:8000",
            "username": "u",
            "password": "p",
        },
    )
    case = SimpleNamespace(expected_tool_params={}, config={})
    report = await ensure_markets_for_case(case)
    assert report.ok
    assert not report.needed


async def test_ensure_warms_until_rules_and_price(stub, monkeypatch):
    monkeypatch.setattr(
        market_warmup,
        "staging_config",
        lambda: {
            "api_url": "http://staging:8000",
            "username": "u",
            "password": "p",
        },
    )
    state = {"rules_hits": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/market-data/trading-pair/add":
            return _json(
                {
                    "success": True,
                    "connector_name": "xrpl",
                    "trading_pair": "RLUSD-XRP",
                    "message": "ok",
                }
            )
        if request.url.path == "/connectors/xrpl/trading-rules":
            state["rules_hits"] += 1
            if state["rules_hits"] < 2:
                return _json({})
            return _json(
                {
                    "RLUSD-XRP": {
                        "min_order_size": 1e-6,
                        "buy_order_collateral_token": "XRP",
                    }
                }
            )
        if request.url.path == "/market-data/prices":
            return _json({"connector": "xrpl", "prices": {"RLUSD-XRP": 0.978}})
        return _json({"detail": "not found"}, 404)

    seen = stub(handler)
    case = SimpleNamespace(
        expected_tool_params={
            "manage_executors": {
                "connector_name": "xrpl",
                "trading_pair": "RLUSD-XRP",
            }
        },
        config={},
    )
    report = await ensure_markets_for_case(case, timeout_s=5.0, poll_s=0.01)
    assert report.ok, report.detail
    assert report.warmed == [MarketRef("xrpl", "RLUSD-XRP")]
    paths = [p for _, p, _ in seen]
    assert "/market-data/trading-pair/add" in paths
    assert "/connectors/xrpl/trading-rules" in paths
    assert "/market-data/prices" in paths


async def test_ensure_failure_is_harness_artifact(stub, monkeypatch):
    monkeypatch.setattr(
        market_warmup,
        "staging_config",
        lambda: {
            "api_url": "http://staging:8000",
            "username": "u",
            "password": "p",
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/market-data/trading-pair/add":
            return _json({"detail": "boom"}, 500)
        return _json({})

    stub(handler)
    case = SimpleNamespace(
        id="agent_condor_005",
        type="consult",
        category="executors",
        domain="general_consult",
        risk_level="destructive",
        expected_tools=["manage_executors"],
        expected_tool_params={
            "manage_executors": {
                "connector_name": "xrpl",
                "trading_pair": "RLUSD-XRP",
            }
        },
        config={},
    )
    report = await ensure_markets_for_case(case, timeout_s=2.0, poll_s=0.01)
    assert not report.ok
    card = warmup_failure_card(case, "anthropic:claude-sonnet-5", report)
    assert card.harness_artifact and "market warmup failed" in card.harness_artifact
    assert card.case_id == "agent_condor_005"
    assert card.composite == 0.0


def test_warmup_report_detail_when_clean():
    report = WarmupReport(
        markets=[MarketRef("xrpl", "RLUSD-XRP")],
        warmed=[MarketRef("xrpl", "RLUSD-XRP")],
    )
    assert report.ok
    assert report.harness_artifact_reason() is None
    assert "warmed 1" in report.detail
