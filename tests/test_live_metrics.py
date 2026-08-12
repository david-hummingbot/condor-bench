"""Param matching and live-response validity: the two metrics tool-name F1 misses.

The failure both exist to catch is the same one: a model that picks the right tool
and accomplishes nothing. ``metrics/tool_accuracy`` scores that 1.0.
"""

from __future__ import annotations

import pytest

from metrics.live_validity import LiveValidityMetric, validity_breakdown
from metrics.tool_params import ToolParamMetric, param_breakdown

params = ToolParamMetric()
validity = LiveValidityMetric()


def call(tool: str, args) -> dict:
    return {"tool": tool, "args": args}


# ── Param matching ─────────────────────────────────────────────────────────────
def test_exact_match_scores_one():
    calls = [call("get_market_data", {"connector_name": "binance", "trading_pair": "BTC-USDT"})]
    assert params.score(calls, {"get_market_data": {"trading_pair": "BTC-USDT"}}) == 1.0


def test_partial_match_is_proportional():
    calls = [call("get_market_data", {"connector_name": "binance", "trading_pair": "ETHUSD"})]
    score = params.score(
        calls,
        {"get_market_data": {"connector_name": "binance", "trading_pair": "BTC-USDT"}},
    )
    assert score == pytest.approx(0.5)


def test_uncalled_tool_scores_zero():
    assert params.score([], {"get_market_data": {"trading_pair": "BTC-USDT"}}) == 0.0


def test_no_pinned_params_returns_none():
    """None, not 1.0 — the weight is redistributed rather than credited."""
    assert params.score([call("x", {})], {}) is None


def test_json_string_args_are_parsed():
    """OpenAI-compatible providers deliver tool args as a JSON string."""
    calls = [call("get_market_data", '{"trading_pair": "BTC-USDT"}')]
    assert params.score(calls, {"get_market_data": {"trading_pair": "BTC-USDT"}}) == 1.0


def test_stringified_numbers_match():
    """A model that emitted the right number shouldn't lose to the transport."""
    calls = [call("set_account_position_mode_and_leverage", {"leverage": "3"})]
    assert params.score(
        calls, {"set_account_position_mode_and_leverage": {"leverage": 3}}
    ) == 1.0


def test_scalar_matches_single_element_list():
    """Several condor tools accept trading_pair or trading_pairs for one intent."""
    assert params.score(
        [call("get_market_data", {"trading_pairs": ["BTC-USDT"]})],
        {"get_market_data": {"trading_pair": "BTC-USDT"}},
    ) == 1.0
    assert params.score(
        [call("get_market_data", {"trading_pairs": "BTC-USDT"})],
        {"get_market_data": {"trading_pairs": ["BTC-USDT"]}},
    ) == 1.0


def test_string_match_is_case_insensitive():
    assert params.score(
        [call("get_market_data", {"connector_name": "Binance"})],
        {"get_market_data": {"connector_name": "binance"}},
    ) == 1.0


def test_bool_is_not_confused_with_one():
    """In Python True == 1, but passing 1 where a bool is wanted is a different call."""
    assert params.score(
        [call("get_portfolio_overview", {"include_balances": 1})],
        {"get_portfolio_overview": {"include_balances": True}},
    ) == 0.0
    assert params.score(
        [call("get_portfolio_overview", {"include_balances": "true"})],
        {"get_portfolio_overview": {"include_balances": True}},
    ) == 1.0


def test_list_match_is_order_insensitive():
    assert params.score(
        [call("search_history", {"trading_pairs": ["ETH-USDT", "BTC-USDT"]})],
        {"search_history": {"trading_pairs": ["BTC-USDT", "ETH-USDT"]}},
    ) == 1.0


def test_nested_dict_is_a_subset_match():
    """A case can pin one key of a config without freezing the whole object."""
    assert params.score(
        [call("manage_executors", {"executor_config": {"type": "grid", "levels": 10}})],
        {"manage_executors": {"executor_config": {"type": "grid"}}},
    ) == 1.0


def test_best_matching_call_is_scored():
    """A model that lists then creates is scored on the call the case is about."""
    calls = [
        call("manage_executors", {"action": "get_all_bots"}),
        call("manage_executors", {"action": "create", "trading_pair": "SOL-USDT"}),
    ]
    assert params.score(
        calls, {"manage_executors": {"action": "create", "trading_pair": "SOL-USDT"}}
    ) == 1.0


def test_param_breakdown_names_the_mismatch():
    detail = param_breakdown(
        [call("get_market_data", {"trading_pair": "ETHUSD"})],
        {"get_market_data": {"trading_pair": "BTC-USDT"}},
    )
    assert detail["get_market_data"]["called"] is True
    assert detail["get_market_data"]["mismatched"]["trading_pair"]["actual"] == "ETHUSD"


# ── Live validity ──────────────────────────────────────────────────────────────
def response(tool: str, output) -> dict:
    return {"tool": tool, "output": output}


def test_error_payload_scores_zero():
    """The failure mode: right tool, plausible args, error every time."""
    assert validity.score([response("get_market_data", "Error: 401 Unauthorized")]) == 0.0


def test_error_field_in_json_scores_zero():
    assert validity.score(
        [response("manage_bots", '{"error": "bot not found"}')]
    ) == 0.0


def test_status_code_above_400_scores_zero():
    assert validity.score([response("manage_bots", {"status_code": 500})]) == 0.0


def test_empty_payload_is_not_a_working_call():
    assert validity.score([response("get_market_data", "[]")]) == 0.0


def test_healthy_payload_scores_one():
    assert validity.score(
        [response("get_market_data", '{"mid_price": 65000.0}')]
    ) == 1.0


def test_no_tool_calls_returns_none():
    """An advisory consult has nothing to validate — None, not 0."""
    assert validity.score([]) is None


def test_field_assertion_is_checked():
    ok = validity.score(
        [response("get_market_data", '{"mid_price": 65000.0}')],
        {"get_market_data": {"fields": {"mid_price": {"gt": 0}}}},
    )
    bad = validity.score(
        [response("get_market_data", '{"mid_price": 0}')],
        {"get_market_data": {"fields": {"mid_price": {"gt": 0}}}},
    )
    assert ok == 1.0
    assert bad is not None and bad < 1.0


def test_contains_assertion_is_checked():
    assert validity.score(
        [response("get_market_data", '{"candles": [1, 2]}')],
        {"get_market_data": {"contains": ["candle"]}},
    ) == 1.0


def test_assertion_for_an_uncalled_tool_lowers_the_score():
    """A pinned assertion that never ran is a miss, not a silent pass."""
    score = validity.score(
        [response("get_market_data", '{"mid_price": 1}')],
        {
            "get_market_data": {"nonempty": True},
            "manage_executors": {"nonempty": True},
        },
    )
    assert score is not None and score < 1.0


def test_the_word_error_deep_in_a_payload_is_not_a_failure():
    """A long successful payload may mention "error" in a log line or a field name."""
    payload = '{"logs": [' + ", ".join(f'"line {i}"' for i in range(120)) + ', "error: x"]}'
    assert validity.score([response("manage_bots", payload)]) == 1.0


def test_validity_breakdown_reports_the_reason():
    detail = validity_breakdown([response("get_market_data", "Error: timed out")])
    assert detail["responses"][0]["score"] == 0.0
    assert detail["responses"][0]["error"]


# ── Legend text is not an error (regression) ──────────────────────────────────
# Found while monitoring a live run: every `manage_bots` response scored 0.0 for
# live validity. The payload ends with a legend explaining what each controller
# state means, and `\berror\b\s*[:=]` matched "error = performance report failed"
# inside it — so a healthy "0 active bots" answer was recorded as a failed call.
MANAGE_BOTS_OK = (
    '{"result":"Active Bots Status Summary:\\nTotal Active Bots: 0\\n\\n'
    "No active bots found.\\n\\ncontroller state: running = active | "
    "stopped = kill switch on (no new entries) | error = performance report failed | "
    'unknown = controller config unread"}'
)


def test_a_legend_mentioning_error_is_not_a_failed_call():
    from metrics.live_validity import _error_reason

    assert _error_reason(MANAGE_BOTS_OK) is None


def test_the_healthy_manage_bots_payload_scores_full_validity():
    from metrics.live_validity import LiveValidityMetric

    score = LiveValidityMetric().score(
        [{"tool": "manage_bots", "output": MANAGE_BOTS_OK}],
        {"manage_bots": {"nonempty": True}},
    )
    assert score == 1.0


def test_a_real_error_is_still_caught():
    from metrics.live_validity import _error_reason

    assert _error_reason('{"result":"Error: connection refused"}')
    assert _error_reason('{"error":"boom"}')
    assert _error_reason("Traceback (most recent call last):")
    # A single assignment is not a legend, so this must still fail.
    assert _error_reason("error = could not reach the API")


def test_a_legend_does_not_mask_a_real_error_elsewhere():
    """The guard skips the legend line, it does not stop the scan."""
    from metrics.live_validity import _error_reason

    payload = (
        "state: a = one | b = two | error = meaning\n"
        "Error: the request was rejected"
    )
    assert _error_reason(payload)
