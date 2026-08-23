"""Connector availability: probe the target, judge cases against it.

The failure these guard is a case scoring 0 because the box lacks the connector
its question names — a harness artifact that reads as a model failure. Two
distinctions carry most of the weight and are asserted directly: credentialed vs
merely supported, and "not here" vs "could not tell".
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from bench import market_registry
from bench.market_preflight import (
    NO_DEPENDENCY,
    OK,
    REBINDABLE,
    UNKNOWN,
    UNRUNNABLE,
    account_acting_tools,
    check_cases,
    connector_required_tools,
    connectors_in_text,
    gateway_tools,
    judge,
    needs_for_case,
    prose_forms,
)
from bench.market_registry import (
    CEX,
    GATEWAY,
    PERPETUAL,
    SPOT,
    Binding,
    Connector,
    MarketRegistry,
    probe_registry,
)


@pytest.fixture(autouse=True)
def staging_env(monkeypatch):
    monkeypatch.setenv("HUMMINGBOT_API_URL", "http://staging.test:8000")
    monkeypatch.setenv("HUMMINGBOT_USERNAME", "bench")
    monkeypatch.setenv("HUMMINGBOT_PASSWORD", "bench")


@pytest.fixture
def stub(monkeypatch):
    """Route market_registry's AsyncClient at a handler."""

    def install(handler):
        real = httpx.AsyncClient

        def factory(**kwargs):
            return real(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(market_registry.httpx, "AsyncClient", factory)

    return install


def _registry(**kwargs) -> MarketRegistry:
    """A registry with binance_perpetual_testnet + xrpl credentialed."""
    defaults = dict(
        api_url="http://staging.test:8000",
        accounts=("master_account",),
        connectors={
            "binance": Connector("binance"),
            "binance_perpetual": Connector("binance_perpetual"),
            "binance_perpetual_testnet": Connector(
                "binance_perpetual_testnet", credentialed_on=("master_account",)
            ),
            "xrpl": Connector("xrpl", credentialed_on=("master_account",)),
        },
        gateway_ok=False,
        gateway_error="Gateway service is not available",
    )
    defaults.update(kwargs)
    return MarketRegistry(**defaults)


# ── classification ─────────────────────────────────────────────────────────────


def test_perpetual_detected_through_testnet_suffix():
    """A suffix test on `_perpetual` misses exactly the connectors staging has."""
    conn = Connector("binance_perpetual_testnet")
    assert conn.kind == PERPETUAL
    assert conn.is_testnet
    assert Connector("xrpl").kind == SPOT
    assert not Connector("binance_perpetual").is_testnet


def test_binding_label_reads_as_prose():
    binding = Binding(connector="binance_perpetual_testnet", account="master_account")
    assert binding.label == "Binance perpetuals (testnet)"
    assert Binding(connector="kucoin", account="a").label == "Kucoin"


# ── candidate selection ────────────────────────────────────────────────────────


def test_candidates_only_returns_credentialed_by_default():
    reg = _registry()
    assert reg.candidates() == ["binance_perpetual_testnet", "xrpl"]
    assert "binance" in reg.candidates(needs_credentials=False)


def test_candidates_are_deterministic_and_honour_prefer():
    """Two runs against one box must resolve identically or scores diverge."""
    reg = _registry()
    first = reg.candidates(prefer=["xrpl"])
    assert first[0] == "xrpl"
    assert first == reg.candidates(prefer=["xrpl"])


def test_candidates_never_cross_namespaces():
    reg = _registry(
        gateway={"meteora": Connector("meteora", namespace=GATEWAY)},
        gateway_ok=True,
        gateway_error="",
    )
    assert reg.candidates(namespace=GATEWAY, needs_credentials=False) == ["meteora"]
    assert "meteora" not in reg.candidates(needs_credentials=False)


# ── resolution ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_rejects_a_connector_that_cannot_trade_the_pair(stub):
    """The bug this pins: binding a BTC-USDT grid onto XRPL because it was first."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "xrpl" in request.url.path:
            return httpx.Response(200, json={"RLUSD-XRP": {}})
        return httpx.Response(200, json={"BTC-USDT": {}, "ETH-USDT": {}})

    stub(handler)
    reg = _registry()
    binding = await reg.resolve(prefer=["xrpl"], pair="BTC-USDT")
    assert binding is not None
    assert binding.connector == "binance_perpetual_testnet"


@pytest.mark.asyncio
async def test_resolve_returns_none_when_nothing_lists_the_pair(stub):
    stub(lambda request: httpx.Response(200, json={"RLUSD-XRP": {}}))
    assert await _registry().resolve(pair="BTC-USDT") is None


@pytest.mark.asyncio
async def test_empty_trading_rules_forgiven_only_for_the_named_connector(stub):
    """Warmup populates the named connector's rules; a substitute gets no such benefit."""
    stub(lambda request: httpx.Response(200, json={}))
    reg = _registry()
    assert await reg.resolve(pair="RLUSD-XRP", optimistic_for="xrpl") is not None
    assert await reg.resolve(pair="RLUSD-XRP") is None


@pytest.mark.asyncio
async def test_kind_change_is_recorded_not_hidden(stub):
    stub(
        lambda request: httpx.Response(
            200,
            json=(
                {} if "xrpl" in request.url.path else {"BTC-USDT": {}}
            ),
        )
    )
    reg = _registry()
    binding = await reg.resolve(kind=SPOT, pair="BTC-USDT", allow_kind_change=True)
    assert binding is not None
    assert binding.connector == "binance_perpetual_testnet"
    assert binding.kind_change == "spot → perpetual"


@pytest.mark.asyncio
async def test_kind_change_off_by_default(stub):
    stub(
        lambda request: httpx.Response(
            200, json=({} if "xrpl" in request.url.path else {"BTC-USDT": {}})
        )
    )
    assert await _registry().resolve(kind=SPOT, pair="BTC-USDT") is None


@pytest.mark.asyncio
async def test_failed_rules_fetch_is_not_cached_as_empty(stub):
    """A dropped poll must not become "this connector has no markets" for the run."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"BTC-USDT": {}})

    stub(handler)
    reg = _registry()
    assert await reg.pairs("binance_perpetual_testnet") == ()
    assert await reg.pairs("binance_perpetual_testnet") == ("BTC-USDT",)


# ── probing ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_reads_credentials_per_account(stub):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/connectors/":
            return httpx.Response(200, json=["binance", "binance_perpetual_testnet"])
        if path == "/accounts/":
            return httpx.Response(200, json=["master_account"])
        if path == "/accounts/master_account/credentials":
            return httpx.Response(200, json=["binance_perpetual_testnet"])
        if path == "/gateway/connectors":
            return httpx.Response(200, json={"detail": "Gateway service is not available"})
        return httpx.Response(404)

    stub(handler)
    reg = await probe_registry()
    assert reg.ok
    assert reg.credentialed("binance_perpetual_testnet")
    assert reg.supported("binance") and not reg.credentialed("binance")
    assert reg.account_for("binance_perpetual_testnet") == "master_account"


@pytest.mark.asyncio
async def test_unreachable_target_is_not_an_empty_registry(stub):
    """Empty would report every case broken; ok=False reports the target instead."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    stub(handler)
    reg = await probe_registry()
    assert not reg.ok
    assert "refused" in reg.error
    assert not reg.namespace_readable(CEX)


@pytest.mark.asyncio
async def test_gateway_detail_body_is_a_failure_not_a_connector_list(stub):
    """hummingbot-api answers 200 with {"detail": ...} when gateway is down."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/connectors/":
            return httpx.Response(200, json=["binance"])
        if path == "/accounts/":
            return httpx.Response(200, json=["master_account"])
        if path.endswith("/credentials"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"detail": "Gateway service is not available"})

    stub(handler)
    reg = await probe_registry()
    assert reg.ok
    assert not reg.gateway_ok
    assert reg.gateway == {}
    assert "not available" in reg.gateway_error


# ── prose detection ────────────────────────────────────────────────────────────


def test_prose_forms_cover_how_a_case_would_say_it():
    forms = prose_forms("binance_perpetual")
    assert "binance perpetuals" in forms
    assert "binance perps" in forms
    assert "binance futures" in forms


def test_longest_phrase_wins_so_one_mention_is_one_connector():
    """"Binance perpetuals" must not also report spot binance."""
    found = connectors_in_text(
        "Set Binance perpetuals to 3x leverage for BTC-USDT.",
        ["binance", "binance_perpetual", "xrpl"],
    )
    assert found == ["binance_perpetual"]


def test_plain_venue_name_resolves_to_spot():
    assert connectors_in_text(
        "Get the BTC-USDT price on Binance.", ["binance", "binance_perpetual"]
    ) == ["binance"]


def test_substring_of_a_word_is_not_a_connector_mention():
    assert connectors_in_text("Rebalance the kucoinish book", ["kucoin"]) == []


# ── tool classification off the snapshot ───────────────────────────────────────


def test_account_acting_tools_come_from_the_tool_surface():
    """Singular account_name means "acts on one account"; plural is a read filter."""
    acting = account_acting_tools()
    assert "set_account_position_mode_and_leverage" in acting
    assert "manage_executors" in acting
    assert "get_portfolio_overview" not in acting
    assert "search_history" not in acting


def test_gateway_tools_come_from_the_tool_surface():
    dex = gateway_tools()
    assert "explore_dex_pools" in dex
    assert "manage_amm" in dex
    assert "get_market_data" not in dex


# ── verdicts ───────────────────────────────────────────────────────────────────


def _case(**kwargs):
    base = dict(
        id="c1",
        type="tool",
        risk_level="read_only",
        question="",
        expected_tools=[],
        expected_tool_params={},
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def _needs(case, reg=None):
    reg = reg or _registry()
    return needs_for_case(case, sorted(reg.connectors), sorted(reg.gateway))


def test_uncredentialed_connector_with_an_alternative_is_rebindable():
    case = _case(
        question="Set Binance perpetuals to 3x leverage for BTC-USDT.",
        expected_tools=["set_account_position_mode_and_leverage"],
    )
    reg = _registry()
    verdict = judge(_needs(case, reg), reg)
    assert verdict.verdict == REBINDABLE
    assert verdict.suggested == "binance_perpetual_testnet"


def test_no_alternative_at_all_is_unrunnable():
    reg = _registry(
        connectors={
            "binance_perpetual": Connector("binance_perpetual"),
            "xrpl": Connector("xrpl", credentialed_on=("master_account",)),
        }
    )
    case = _case(
        question="Set Binance perpetuals to 3x leverage.",
        expected_tools=["set_account_position_mode_and_leverage"],
    )
    verdict = judge(_needs(case, reg), reg)
    assert verdict.verdict == UNRUNNABLE
    assert "no credentialed perpetual" in verdict.detail


def test_read_only_case_needs_support_not_credentials():
    """Public market data works on an uncredentialed connector, so this is fine."""
    case = _case(question="Price of BTC-USDT on Binance?", expected_tools=["get_market_data"])
    reg = _registry()
    assert judge(_needs(case, reg), reg).verdict == OK


def test_unreadable_target_never_blames_the_dataset(stub):
    reg = MarketRegistry(ok=False, error="unreachable")
    case = _case(question="Set Binance perpetuals to 3x leverage.")
    # Prose cannot be matched with no connector list, so this leans on a pin.
    needs = needs_for_case(
        _case(expected_tool_params={"get_market_data": {"connector_name": "binance"}}), []
    )
    assert judge(needs, reg).verdict == UNKNOWN
    assert judge(needs_for_case(case, []), reg).verdict == NO_DEPENDENCY


def test_dex_case_is_unrunnable_while_the_gateway_is_down():
    """No connector is named and it still cannot run — the tool decides."""
    case = _case(
        question="What DAMM v2 positions do I own on Meteora?",
        expected_tools=["manage_amm"],
    )
    reg = _registry()
    verdict = judge(_needs(case, reg), reg)
    assert verdict.verdict == UNRUNNABLE
    assert "gateway service is unavailable" in verdict.detail


def test_gateway_connector_is_not_rebound_to_an_exchange():
    reg = _registry(
        gateway={"meteora": Connector("meteora", namespace=GATEWAY)},
        gateway_ok=True,
        gateway_error="",
    )
    case = _case(
        expected_tools=["explore_dex_pools"],
        expected_tool_params={"explore_dex_pools": {"connector": "raydium"}},
    )
    verdict = judge(_needs(case, reg), reg)
    assert verdict.verdict == REBINDABLE
    assert verdict.suggested == "meteora"


# ── end to end ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_cases_demotes_rebindable_when_no_pair_fits(stub):
    """Pair-blind judging says rebindable; the pair check is what says otherwise."""
    stub(lambda request: httpx.Response(200, json={"RLUSD-XRP": {}}))
    reg = _registry(
        connectors={
            "binance": Connector("binance"),
            "xrpl": Connector("xrpl", credentialed_on=("master_account",)),
        }
    )
    case = _case(
        id="t001",
        type="tick",
        expected_tools=["manage_executors"],
        expected_tool_params={
            "manage_executors": {"connector_name": "binance", "trading_pair": "BTC-USDT"}
        },
    )
    report = await check_cases([case], registry=reg)
    verdict = report.verdicts[0]
    assert verdict.verdict == UNRUNNABLE
    assert "BTC-USDT" in verdict.detail
    assert verdict.suggested == ""


@pytest.mark.asyncio
async def test_check_cases_reports_the_pair_with_the_suggestion(stub):
    stub(
        lambda request: httpx.Response(
            200, json=({} if "xrpl" in request.url.path else {"BTC-USDT": {}})
        )
    )
    reg = _registry()
    case = _case(
        id="t001",
        type="tick",
        expected_tools=["manage_executors"],
        expected_tool_params={
            "manage_executors": {"connector_name": "binance", "trading_pair": "BTC-USDT"}
        },
    )
    report = await check_cases([case], registry=reg)
    assert report.verdicts[0].suggested == "binance_perpetual_testnet/BTC-USDT"
    assert "spot → perpetual" in report.verdicts[0].detail


@pytest.mark.asyncio
async def test_report_counts_and_dict_shape(stub):
    stub(lambda request: httpx.Response(200, json={"BTC-USDT": {}}))
    reg = _registry()
    cases = [
        _case(id="ok1", question="Price of BTC-USDT on Binance?", expected_tools=["get_market_data"]),
        _case(id="none1", question="Explain funding rates."),
    ]
    report = await check_cases(cases, registry=reg)
    assert report.counts[OK] == 1
    assert report.counts[NO_DEPENDENCY] == 1
    assert report.affected == []
    payload = report.as_dict()
    assert payload["credentialed"]["xrpl"] == ["master_account"]
    assert {c["case_id"] for c in payload["cases"]} == {"ok1", "none1"}


def test_case_naming_no_connector_still_needs_a_credentialed_one():
    """"Put BTC-USDT perpetuals back to 2x leverage" names no venue and still needs keys."""
    case = _case(
        id="tool_set_leverage_003",
        question="Put BTC-USDT perpetuals back to 2x leverage.",
        expected_tools=["set_account_position_mode_and_leverage"],
    )
    reg = _registry()
    verdict = judge(_needs(case, reg), reg)
    assert verdict.verdict == OK
    assert "binance_perpetual_testnet" in verdict.detail


def test_implied_perpetual_need_is_unrunnable_with_only_spot_keys():
    """The state this box was in before a perp credential existed."""
    reg = _registry(
        connectors={
            "binance_perpetual": Connector("binance_perpetual"),
            "xrpl": Connector("xrpl", credentialed_on=("master_account",)),
        }
    )
    case = _case(
        question="Put BTC-USDT perpetuals back to 2x leverage.",
        expected_tools=["set_account_position_mode_and_leverage"],
    )
    verdict = judge(_needs(case, reg), reg)
    assert verdict.verdict == UNRUNNABLE
    assert "no credentialed perpetual connector" in verdict.detail


def test_a_named_connector_beats_the_implied_check():
    """A case that pins a connector is judged on it, not on what the box has."""
    case = _case(
        question="Set Binance perpetuals to 3x leverage.",
        expected_tools=["set_account_position_mode_and_leverage"],
    )
    needs = _needs(case)
    assert needs.implied_tools == ()
    assert [ref.connector for ref in needs.refs] == ["binance_perpetual"]


def test_connector_required_tools_come_from_the_tool_surface():
    required = connector_required_tools()
    assert "set_account_position_mode_and_leverage" in required
    assert "manage_bots" not in required


@pytest.mark.asyncio
async def test_gateway_connector_objects_are_parsed(stub):
    """The real /gateway/connectors shape: objects, not strings."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/connectors/":
            return httpx.Response(200, json=["binance"])
        if path == "/accounts/":
            return httpx.Response(200, json=["master_account"])
        if path.endswith("/credentials"):
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json={
                "connectors": [
                    {
                        "name": "meteora",
                        "trading_types": ["clmm"],
                        "chain": "solana",
                        "networks": ["devnet", "mainnet-beta"],
                    },
                    {"name": "raydium", "trading_types": ["amm", "clmm"]},
                ]
            },
        )

    stub(handler)
    reg = await probe_registry()
    assert reg.gateway_ok, reg.gateway_error
    assert sorted(reg.gateway) == ["meteora", "raydium"]
    assert reg.supported("meteora", GATEWAY)
    assert not reg.supported("meteora")


@pytest.mark.asyncio
async def test_unparseable_gateway_payload_is_not_reported_as_empty(stub):
    """A shape we don't understand must accuse the parser, not the container."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/connectors/":
            return httpx.Response(200, json=["binance"])
        if path == "/accounts/":
            return httpx.Response(200, json=["master_account"])
        if path.endswith("/credentials"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"connectors": [[1, 2, 3]]})

    stub(handler)
    reg = await probe_registry()
    assert not reg.gateway_ok
    assert "not understood" in reg.gateway_error


@pytest.mark.asyncio
async def test_genuinely_empty_gateway_is_readable(stub):
    """Zero configured connectors is an answer, not a failure to get one."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/connectors/":
            return httpx.Response(200, json=["binance"])
        if path == "/accounts/":
            return httpx.Response(200, json=["master_account"])
        if path.endswith("/credentials"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[])

    stub(handler)
    reg = await probe_registry()
    assert reg.gateway_ok
    assert reg.gateway == {}
