"""A run that reported tokens but no cost used to be reported as costing nothing.

Only the pydantic-ai client priced its own runs. The ACP bridges pass a cost
through when the agent volunteers one, and Claude Code's does not — so every ACP
row carried tokens with an empty price, the matrix averaged cost over the API
rows alone, and the router compared a priced model against an "unpriced" one.

The pricing itself is the offline `genai-prices` snapshot; what is pinned here is
bench's part: which key gets looked up, and that an unknown model stays absent
rather than becoming 0.0.
"""

from __future__ import annotations

from bench.client import _fill_estimated_cost, _resolve_acp_alias

USAGE = {"input_tokens": 100_000, "output_tokens": 500}


def _cost(key: str, **extra) -> float | None:
    """`extra` goes into the usage dict — including `model`, the id an agent
    reports for itself, which is a different thing from the configured key."""
    usage = {**USAGE, **extra}
    _fill_estimated_cost(usage, key)
    return usage.get("cost_usd")


def test_an_acp_alias_is_priced_as_the_model_it_means():
    """`claude-code:sonnet` is billed as Sonnet 5; the dataset never heard of it."""
    assert _cost("claude-code:sonnet") == _cost("anthropic:claude-sonnet-5")


def test_the_long_context_marker_does_not_defeat_the_lookup():
    assert _cost("claude-code:opus[1m]") == _cost("anthropic:claude-opus-5")


def test_an_alias_that_is_already_a_real_id_passes_through():
    assert _cost("claude-code:claude-sonnet-5") == _cost("anthropic:claude-sonnet-5")


def test_the_cli_default_is_priced():
    """The common selection. Unpriced here means most ACP runs report no cost."""
    assert _cost("claude-code:default") == _cost("anthropic:claude-sonnet-5")


def test_a_bare_agent_key_names_no_model_so_it_is_not_priced():
    """Nothing reports which model the CLI chose, and a guess would be a wrong price."""
    assert _cost("claude-code") is None


def test_a_model_the_agent_named_for_itself_beats_the_configured_key():
    assert _cost("claude-code", model="claude-haiku-4-5") == _cost(
        "anthropic:claude-haiku-4-5"
    )


def test_a_bridge_reported_cost_is_never_overwritten():
    """That one is real money; this module only ever estimates."""
    assert _cost("claude-code:sonnet", cost_usd=0.42) == 0.42


def test_an_unpriced_backend_stays_unpriced_rather_than_free():
    assert _cost("ollama:llama3.2:3b") is None


def test_a_run_with_no_usage_at_all_is_left_alone():
    usage: dict = {}
    _fill_estimated_cost(usage, "claude-code:sonnet")
    assert usage == {}


def test_non_acp_keys_are_unaffected_by_alias_resolution():
    assert _cost("anthropic:claude-sonnet-5") is not None
    assert _cost("openai:gpt-4o") is not None


def test_an_unknown_alias_is_left_for_the_dataset_to_reject():
    """A missing table entry costs a price, never a wrong one."""
    assert _resolve_acp_alias("some-future-model") == "some-future-model"
    assert _cost("claude-code:some-future-model") is None


def test_a_turn_the_bridge_never_billed_is_not_recorded_as_free():
    """claude-agent-acp answers a rejected model id with cost 0 and no tokens, and
    sets no error on the turn — so a $0.00 row would enter the cost averages."""
    usage = {"cost_usd": 0, "context_used": 0, "context_size": 200000}
    _fill_estimated_cost(usage, "claude-code:haiku")
    assert "cost_usd" not in usage


def test_a_genuine_zero_with_tokens_behind_it_is_kept():
    """A free-tier row is a real measurement; only the tokenless zero is bogus."""
    usage = {"cost_usd": 0, "input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    _fill_estimated_cost(usage, "openrouter:some/free-model")
    assert usage["cost_usd"] == 0
