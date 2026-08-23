"""Declared markets: parse, bind, substitute, or refuse the run.

The property that matters most is that the question and the ground truth never
disagree. A case asking about one connector while being scored against another
is worse than a case that fails to run, because it looks like a model error.
"""

from __future__ import annotations

import pytest

from bench.dataset import load_all_cases
from bench.market_registry import CEX, PERPETUAL, Binding, Connector
from bench.market_resolver import (
    MarketsUnavailable,
    Requirement,
    _ATTRS,
    assert_resolvable,
    bindings_summary,
    nominal_binding,
    prepare_cases,
    render_nominal,
    requirements_for,
    resolve_case,
    substitute,
    unresolved_placeholders,
)

from tests.test_market_registry import _registry, staging_env, stub  # noqa: F401


class _Case:
    """Minimal stand-in with the fields substitution walks."""

    def __init__(self, **kwargs):
        self.id = kwargs.pop("id", "c1")
        self.type = kwargs.pop("type", "tool")
        self.question = kwargs.pop("question", "")
        self.markets = kwargs.pop("markets", {})
        self.expected_tool_params = kwargs.pop("expected_tool_params", {})
        self.config = kwargs.pop("config", {})
        for key, value in kwargs.items():
            setattr(self, key, value)


def _dataclass_case(**kwargs):
    """A real ToolCase, because substitution uses dataclasses.replace."""
    from bench.dataset import ToolCase

    base = dict(id="c1", tool="set_account_position_mode_and_leverage", question="")
    base.update(kwargs)
    return ToolCase(**base)


# ── parsing ────────────────────────────────────────────────────────────────────


def test_requirement_defaults_to_needing_credentials():
    req = Requirement.parse("perp", {"kind": "perpetual"})
    assert req.needs == "credentials"
    assert req.needs_credentials
    assert req.namespace == CEX
    assert req.kind == PERPETUAL


def test_bare_string_is_shorthand_for_prefer():
    req = Requirement.parse("venue", "binance")
    assert req.prefer == ("binance",)


def test_pair_and_pairs_are_interchangeable():
    assert Requirement.parse("v", {"pair": "BTC-USDT"}).pairs == ("BTC-USDT",)
    assert Requirement.parse("v", {"pairs": ["BTC-USDT", "ETH-USDT"]}).pairs == (
        "BTC-USDT",
        "ETH-USDT",
    )


def test_gateway_requirement_does_not_demand_account_credentials():
    """DEX connectors are keyed by a wallet; requiring credentials rejects all."""
    req = Requirement.parse("dex", {"namespace": "gateway"})
    assert not req.needs_credentials


@pytest.mark.parametrize(
    "spec",
    [
        {"kind": "futures"},
        {"needs": "maybe"},
        {"namespace": "dex"},
        ["binance"],
    ],
)
def test_malformed_requirements_are_rejected_loudly(spec):
    with pytest.raises(ValueError):
        Requirement.parse("v", spec)


def test_requirements_for_reads_declaration_order():
    case = _Case(markets={"a": {}, "b": {}})
    assert [r.name for r in requirements_for(case)] == ["a", "b"]


# ── substitution ───────────────────────────────────────────────────────────────


def test_every_placeholder_attribute_resolves_on_a_binding():
    """The drift that emptied {perp.pair}: the vocabulary outran Binding."""
    binding = Binding(
        connector="binance_perpetual_testnet",
        account="master_account",
        trading_pair="BTC-USDT",
    )
    for attr in _ATTRS:
        assert getattr(binding, attr), f"{attr} resolved empty"


def test_unknown_attribute_survives_substitution_to_be_caught_later():
    """It must not blank out: an untouched token is what fails the run."""
    binding = Binding(connector="binance", account="a")
    assert substitute("{v.nope}", {"v": binding}) == "{v.nope}"


def test_substitution_reaches_nested_ground_truth():
    binding = Binding(
        connector="binance_perpetual_testnet", account="m", trading_pair="BTC-USDT"
    )
    out = substitute(
        {
            "q": "Set {v.label} for {v.pair}",
            "params": {"tool": {"trading_pair": "{v.pair}", "leverage": 3}},
            "steps": [{"expected_tools": ["x"], "note": "{v.connector}"}],
        },
        {"v": binding},
    )
    assert out["q"] == "Set Binance perpetuals (testnet) for BTC-USDT"
    assert out["params"]["tool"] == {"trading_pair": "BTC-USDT", "leverage": 3}
    assert out["steps"][0]["note"] == "binance_perpetual_testnet"


def test_int_params_stay_ints():
    binding = Binding(connector="binance", account="a", trading_pair="BTC-USDT")
    out = substitute({"leverage": 3, "pair": "{v.pair}"}, {"v": binding})
    assert out["leverage"] == 3 and isinstance(out["leverage"], int)


def test_unresolved_placeholders_are_reported():
    case = _dataclass_case(question="Set {perp.label} for {perp.pair}")
    assert unresolved_placeholders(case) == ["{perp.label}", "{perp.pair}"]


# ── resolution ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_question_and_ground_truth_bind_to_the_same_market(stub):  # noqa: F811
    stub(lambda request: __import__("httpx").Response(200, json={"BTC-USDT": {}}))
    case = _dataclass_case(
        markets={"perp": {"kind": "perpetual", "prefer": ["binance_perpetual"], "pair": "BTC-USDT"}},
        question="Set {perp.label} to 3x leverage for {perp.pair}.",
        expected_tool_params={
            "set_account_position_mode_and_leverage": {
                "leverage": 3,
                "trading_pair": "{perp.pair}",
                "connector_name": "{perp.connector}",
            }
        },
    )
    resolution = await resolve_case(case, _registry())
    assert resolution.ok
    params = resolution.case.expected_tool_params["set_account_position_mode_and_leverage"]
    assert params["connector_name"] == "binance_perpetual_testnet"
    assert params["trading_pair"] == "BTC-USDT"
    assert "Binance perpetuals (testnet)" in resolution.case.question
    assert "BTC-USDT" in resolution.case.question


@pytest.mark.asyncio
async def test_case_without_markets_is_returned_untouched():
    case = _dataclass_case(question="Set Binance perpetuals to 3x leverage.")
    resolution = await resolve_case(case, _registry())
    assert resolution.ok
    assert not resolution.declared
    assert resolution.case is case


@pytest.mark.asyncio
async def test_unmet_requirement_reports_why(stub):  # noqa: F811
    stub(lambda request: __import__("httpx").Response(200, json={"RLUSD-XRP": {}}))
    reg = _registry(
        connectors={"xrpl": Connector("xrpl", credentialed_on=("master_account",))}
    )
    case = _dataclass_case(
        markets={"perp": {"kind": "perpetual", "pair": "BTC-USDT"}},
        question="Set {perp.label} to 3x leverage.",
    )
    resolution = await resolve_case(case, reg)
    assert not resolution.ok
    assert "perpetual" in resolution.reason()
    # The unsubstituted case is returned, never a half-bound one.
    assert "{perp.label}" in resolution.case.question


@pytest.mark.asyncio
async def test_placeholder_with_no_requirement_is_a_dataset_bug(stub):  # noqa: F811
    stub(lambda request: __import__("httpx").Response(200, json={"BTC-USDT": {}}))
    case = _dataclass_case(
        markets={"perp": {"kind": "perpetual", "pair": "BTC-USDT"}},
        question="Set {perp.label} on {spot.label}.",
    )
    resolution = await resolve_case(case, _registry())
    assert not resolution.ok
    assert "{spot.label}" in resolution.reason()


@pytest.mark.asyncio
async def test_unreadable_namespace_is_reported_as_such(stub):  # noqa: F811
    stub(lambda request: __import__("httpx").Response(200, json={}))
    case = _dataclass_case(
        markets={"dex": {"namespace": "gateway"}}, question="Pools on {dex.label}?"
    )
    resolution = await resolve_case(case, _registry())
    assert not resolution.ok
    assert "unreadable" in resolution.reason()


@pytest.mark.asyncio
async def test_kind_change_is_noted_on_the_resolution(stub):  # noqa: F811
    import httpx

    stub(
        lambda request: httpx.Response(
            200, json=({} if "xrpl" in request.url.path else {"BTC-USDT": {}})
        )
    )
    case = _dataclass_case(
        markets={
            "venue": {
                "kind": "spot",
                "prefer": ["binance"],
                "pair": "BTC-USDT",
                "allow_kind_change": True,
            }
        },
        question="Grid on {venue.label}.",
    )
    resolution = await resolve_case(case, _registry())
    assert resolution.ok
    assert resolution.notes == ["venue crossed spot → perpetual"]


# ── the gate ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prepare_cases_refuses_the_run_when_a_market_is_missing(stub):  # noqa: F811
    stub(lambda request: __import__("httpx").Response(200, json={"RLUSD-XRP": {}}))
    reg = _registry(
        connectors={"xrpl": Connector("xrpl", credentialed_on=("master_account",))}
    )
    case = _dataclass_case(
        id="tool_set_leverage_001",
        markets={"perp": {"kind": "perpetual", "pair": "BTC-USDT"}},
        question="Set {perp.label} to 3x leverage.",
    )
    from bench.market_resolver import resolve_cases

    _, resolutions = await resolve_cases([case], registry=reg)
    with pytest.raises(MarketsUnavailable) as exc:
        assert_resolvable(resolutions)
    message = str(exc.value)
    assert "tool_set_leverage_001" in message
    # The message has to say what to do, or the gate just blocks people.
    assert "market-check" in message
    assert "--layers" in message


def test_assert_resolvable_is_silent_when_nothing_declared():
    assert_resolvable({}) is None


@pytest.mark.asyncio
async def test_bindings_summary_records_what_a_score_was_earned_against(stub):  # noqa: F811
    stub(lambda request: __import__("httpx").Response(200, json={"BTC-USDT": {}}))
    case = _dataclass_case(
        markets={"perp": {"kind": "perpetual", "prefer": ["binance_perpetual"], "pair": "BTC-USDT"}},
        question="Set {perp.label}.",
    )
    _, resolutions = await prepare_cases([case], registry=_registry())
    summary = bindings_summary(resolutions)
    assert summary["connectors"] == ["binance_perpetual_testnet"]
    assert summary["cases"]["c1"]["perp"]["trading_pair"] == "BTC-USDT"
    assert summary["cases"]["c1"]["perp"]["is_testnet"] is True


# ── offline rendering ──────────────────────────────────────────────────────────


def test_nominal_binding_uses_the_declared_preference():
    req = Requirement.parse(
        "perp", {"prefer": ["binance_perpetual", "binance_perpetual_testnet"], "pair": "BTC-USDT"}
    )
    binding = nominal_binding(req)
    assert binding.connector == "binance_perpetual"
    assert binding.label == "Binance perpetuals"
    assert binding.pair == "BTC-USDT"


def test_render_nominal_needs_no_target():
    case = _dataclass_case(
        markets={"perp": {"prefer": ["binance_perpetual"], "pair": "BTC-USDT"}},
        question="Set {perp.label} to 3x leverage for {perp.pair}.",
    )
    rendered = render_nominal(case)
    assert rendered.question == "Set Binance perpetuals to 3x leverage for BTC-USDT."
    assert unresolved_placeholders(rendered) == []


# ── the real dataset ───────────────────────────────────────────────────────────


def test_every_declared_market_in_the_dataset_parses():
    """A malformed markets block must fail here, not mid-run."""
    declared = [c for c in load_all_cases() if getattr(c, "markets", None)]
    assert declared, "expected the converted cases to declare markets"
    for case in declared:
        requirements = requirements_for(case)
        assert requirements, case.id


def test_no_dataset_case_has_a_placeholder_without_a_requirement():
    """The typo class that would otherwise be scored as if the dataset meant it."""
    for case in load_all_cases():
        rendered = render_nominal(case)
        assert unresolved_placeholders(rendered) == [], case.id


def test_a_case_that_pins_a_connector_names_it_in_the_question():
    """The ambiguity that cost `tool_manage_executors_002` a real run.

    The prose label "Binance perpetuals (testnet)" does not say whether the id is
    `binance_perpetual` or `binance_perpetual_testnet`. A model that guesses the
    former hits hummingbot-api's uncredentialed 500 ('NoneType' object has no
    attribute 'encode') and is scored for a venue guess instead of for the tool
    under test. So a case pinning `connector_name` must name it outright.
    """
    import re

    def squash(text: str) -> str:
        """Lowercase alphanumerics only, so case and separators stop mattering."""
        return re.sub(r"[^a-z0-9]", "", str(text).lower())

    rendered = {c.id: c for c in (render_nominal(c) for c in load_all_cases())}
    offenders = []
    for case in rendered.values():
        for args in (getattr(case, "expected_tool_params", None) or {}).values():
            if not isinstance(args, dict):
                continue
            connector = args.get("connector_name") or args.get("connector")
            if not connector:
                continue
            text = getattr(case, "question", "") or getattr(case, "scenario_name", "")
            config = getattr(case, "config", None) or {}
            # A tick carries the connector in its config, which the prompt shows
            # the model verbatim, so its prose does not have to repeat it.
            if connector in str(list(config.values())):
                continue
            # "on XRPL" names `xrpl` — same word, different case, no other
            # connector it could mean. "Binance perpetuals (testnet)" does not
            # name `binance_perpetual_testnet`: the words differ from the id and
            # three real connectors start with "binance".
            if squash(connector) not in squash(text):
                offenders.append(f"{case.id}: {connector!r} not named in {text!r}")
    assert not offenders, offenders


def test_the_venue_inference_case_stays_ambiguous():
    """`_003` names no venue on purpose — that is the inference it tests."""
    rendered = {c.id: c for c in (render_nominal(c) for c in load_all_cases())}
    case = rendered["tool_set_leverage_003"]
    assert "connector" not in case.question
    params = case.expected_tool_params["set_account_position_mode_and_leverage"]
    assert "connector_name" not in params, "pinning it would defeat the point"


def test_the_label_still_reads_as_prose_where_nothing_is_pinned():
    """A tick's instructions name the venue in words, not as an id."""
    rendered = {c.id: c for c in (render_nominal(c) for c in load_all_cases())}
    assert "on Binance." in rendered["t001"].strategy_instructions
