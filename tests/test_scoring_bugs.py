"""Eight ways the harness mismeasured a correct answer.

An audit of one 81-case run classified every sub-1.0 score. Five were genuine model
failures; sixteen were bad ground truth; eight were the harness scoring itself wrong.
These are those eight, each pinned against the case that exposed it.

The shape they share: a signal that means "we could not measure this" was recorded as
"the model did it badly". A 0.0 is a claim about the model, and every one of these made
that claim on evidence about the harness.
"""

from __future__ import annotations

import json

import pytest

from bench.client import BenchmarkResult, TurnResult, _asks_confirmation
from bench.scorer import _composite
from bench.tool_digest import annotate_epochs, digest_tool_output
from metrics.answer_quality import is_infra_failure
from metrics.live_validity import _error_reason
from metrics.tool_params import ToolParamMetric


# ── 1. The infra gate read tool output ────────────────────────────────────────
def test_a_skill_that_mentions_rate_limits_is_not_a_provider_outage():
    """`agent_market_making_expert_002` → 0.5132, `tool_manage_skill_001` → 0.452.

    Both answered completely, every tool call succeeded, and both were scored as
    provider failures because a skill file condor's own tool returned discusses rate
    limits in prose.
    """
    from metrics.answer_quality import AnswerQualityMetric

    # The detector is a pattern matcher and stays one — "rate limit" really is an infra
    # phrase. What changed is *which text it is handed*: the model's own response, not
    # the transcript that carries tool output.
    skill_text = (
        "replaces open orders; lower = tighter to mid price but more rate limit usage"
    )
    assert is_infra_failure(skill_text), "the pattern itself is unchanged"

    transcript = f"--- Turn 1 ---\nResponse:\nUse 0.2% spread.\nTool log:\n  {skill_text}"
    metric = AnswerQualityMetric()

    class _Judge:
        def generate(self, _prompt):
            return '{"score": 0.9, "reason": "grounded"}'

    metric._judge = _Judge()
    score, reason = metric.score("q", transcript, response="Use 0.2% spread.")
    assert score == pytest.approx(0.9), (
        f"the tool's own text tripped the gate: {reason}"
    )

    # A real provider failure in the model's output still short-circuits, unscored.
    score, reason = metric.score(
        "q", transcript, response="request_limit exceeded before any response"
    )
    assert score is None
    assert "Infrastructure error" in reason


# ── 2. Unjudgeable quality must redistribute, not score zero ──────────────────
def test_quality_none_renormalises_instead_of_capping_the_case_at_055():
    """answer_quality is the absorber every other unscorable metric folds into.

    So a None there could not fall through to 0.0: that keeps the absorbed weight in
    the denominator and scores it zero, capping a flawless case at 0.55.
    """
    perfect = dict(
        answer_quality=1.0,
        tool_accuracy=1.0,
        tool_params=1.0,
        live_validity=1.0,
        latency_score=1.0,
    )
    assert _composite(perfect) == pytest.approx(1.0)

    unjudged = {**perfect, "answer_quality": None}
    assert _composite(unjudged) == pytest.approx(1.0), (
        "a case the judge could not score, but which got everything else right, "
        "is not a 0.55 case"
    )
    # Still reflects the parts that *were* measured.
    mixed = dict(
        answer_quality=None,
        tool_accuracy=1.0,
        tool_params=0.0,
        live_validity=1.0,
        latency_score=0.5,
    )
    assert _composite(mixed) == pytest.approx(0.6364, abs=1e-3)
    # Nothing measurable at all — the caller marks these rows for exclusion.
    assert _composite({"answer_quality": None}) == 0.0


def test_a_malformed_judge_reply_is_not_a_model_failure():
    """`tool_explore_geckoterminal_002` lost the full 0.45 weight to the judge's JSON.

    The answer named the right network and the right top pool, and the one tool call
    scored live 1.0. `Judge error: Expecting ',' delimiter` says nothing about any of
    that, so it must not be scored as if it did.
    """
    from metrics.answer_quality import AnswerQualityMetric

    metric = AnswerQualityMetric()

    class _Boom:
        def generate(self, _prompt):
            raise ValueError("Expecting ',' delimiter: line 1 column 371")

    metric._judge = _Boom()
    score, reason = metric.score("q", "a real transcript")
    assert score is None, "a judge that did not answer must not produce a 0.0"
    assert "not scored" in reason


# ── 3. Lists were shown to the judge as a count ───────────────────────────────
def test_the_judge_can_read_the_rows_it_is_asked_to_verify():
    """`agent_condor_routine_003` → 0.55 for "unverified implementation details".

    The names it cited were real; `manage_routines(action="list")` returned 29 rows and
    the digest replaced them with "29 items". The judge is asked to check grounding
    against tool output, so a count is not a usable digest.
    """
    payload = {
        "routines": [
            {"name": "price_monitor", "type": "continuous", "scope": "global"},
            *[
                {"name": f"r{i}", "type": "oneshot", "scope": "global"}
                for i in range(28)
            ],
        ]
    }
    digest = digest_tool_output("manage_routines", json.dumps(payload))
    assert "price_monitor" in digest, "the row the answer cites must be visible"
    assert "type=continuous" in digest
    assert "additional row(s) omitted" in digest, "the remainder must be stated"
    # Even a short list gets rows — the old code collapsed 3 items to a count too.
    small = digest_tool_output(
        "manage_servers", json.dumps({"servers": [{"name": "bench_staging"}]})
    )
    assert "bench_staging" in small


# ── 4. live_validity read hard failures as success ────────────────────────────
@pytest.mark.parametrize(
    "output",
    [
        # Absent Binance credentials. Scored 1.0 before — the run's real blocker.
        'Error creating executor: 500, message="BinanceExchange.__init__() missing 2 '
        "required positional arguments: 'binance_api_key' and 'binance_api_secret'\"",
        "Error executing tool consult: API error (401): Invalid token",
        "Routine 'market_scanner' could not be run: API error (401): Invalid token",
        # These already worked; keep them working.
        "Error: Failed to get schema for grid_strike: 404",
        "Bot 'eth-maker' not found. Available bots: []",
    ],
)
def test_a_failed_call_is_not_valid_live_data(output):
    assert _error_reason(output), f"scored as success: {output[:60]}"


@pytest.mark.parametrize(
    "output",
    [
        '{"result":"Funding Rate for BTC-USDT on binance_perpetual: 0.0082%"}',
        '{"servers":[{"name":"bench_staging","port":8000}],"active_server":"bench_staging"}',
        "Order History ==== No orders found.",
        '{"created":true,"agent_slug":"bench_dca_agent"}',
    ],
)
def test_real_data_is_not_flagged_as_an_error(output):
    assert _error_reason(output) is None, f"false positive: {output[:60]}"


# ── 5. The judge's view was truncated at the tail ─────────────────────────────
def test_every_tool_call_is_named_inside_the_judge_window():
    """`agent_directional_trader_002` → 0.55 for a call that is in the log.

    The judge said `read_file` on `windows_and_costs.md` never happened and the table
    was fabricated. The call is there and the table is verbatim from the file — the
    digest had fallen off the end of an 8000-character window.
    """
    from metrics.answer_quality import JUDGE_INPUT_CHARS

    calls = [
        {"tool": f"manage_skill_{i}", "args": {"action": "read_file"}, "tool_call_id": str(i)}
        for i in range(9)
    ]
    responses = [
        {"tool": f"manage_skill_{i}", "tool_call_id": str(i), "output": "z" * 5000}
        for i in range(9)
    ]
    transcript = BenchmarkResult(
        case_id="x",
        model="m",
        turns=[TurnResult(response="ANSWER", tool_calls=calls, latency_s=1.0,
                          tool_responses=responses)],
    ).transcript_for_judge()

    window = transcript[:JUDGE_INPUT_CHARS]
    assert len(transcript) <= JUDGE_INPUT_CHARS
    for i in range(9):
        assert f"manage_skill_{i}" in window, f"call {i} invisible to the judge"


# ── 6. Confirmation gating was unearnable ─────────────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        "Set ETH-USDT to 5x leverage.\n\nProceed?",          # \b after ? never matched
        "To confirm: set ETH-USDT to 5x leverage?",
        "Want me to set the leverage to 5x?",                 # verb list was closed
        "Should I switch to HEDGE mode first?",
        "Would you like me to apply this?",
        "Shall I proceed?",
    ],
)
def test_a_gated_action_is_recognised_as_asking(text):
    """condor's prompt says "Confirm dangerous actions", so the harness must hear it.

    `tool_set_leverage_001` and `_002` both asked, made zero calls, and scored 0.512
    for obeying their own instructions.
    """
    assert _asks_confirmation(text)


@pytest.mark.parametrize(
    "text",
    [
        "I have set the leverage to 5x.",
        "Here is the funding rate: 0.0082%.",
        "The grid is now live with 10 levels.",
    ],
)
def test_a_completed_action_is_not_mistaken_for_a_question(text):
    assert not _asks_confirmation(text)


# ── 7 & 8. Param pins: nesting and punctuation ────────────────────────────────
def test_a_pin_is_found_inside_the_config_object_the_tool_documents():
    """`tool_manage_executors_002` → 0.568 for putting the values where they belong.

    `manage_executors(action="create")` takes `connector_name`/`trading_pair` inside
    `executor_config`; the top-level keys of those names are the filter args for
    `positions_summary`.
    """
    calls = [
        {
            "tool": "manage_executors",
            "args": {
                "action": "create",
                "executor_type": "grid_executor",
                "executor_config": {
                    "connector_name": "binance",
                    "trading_pair": "SOL-USDT",
                },
            },
        }
    ]
    pins = {
        "manage_executors": {
            "action": "create",
            "connector_name": "binance",
            "trading_pair": "SOL-USDT",
        }
    }
    assert ToolParamMetric().score(calls, pins) == 1.0
    # A wrong nested value is still wrong.
    wrong = [{"tool": "manage_executors", "args": {"executor_config": {"connector_name": "bitget"}}}]
    assert ToolParamMetric().score(wrong, {"manage_executors": {"connector_name": "binance"}}) == 0.0


def test_a_trailing_full_stop_does_not_fail_a_free_text_pin():
    """`tool_send_notification_002` → 0.681 on the question's own punctuation."""
    calls = [{"tool": "send_notification", "args": {"text": "funding check complete."}}]
    pins = {"send_notification": {"text": "funding check complete"}}
    assert ToolParamMetric().score(calls, pins) == 1.0
    # Different text still fails.
    other = [{"tool": "send_notification", "args": {"text": "something else"}}]
    assert ToolParamMetric().score(other, pins) == 0.0


# ── Kept from the epoch fix, which came out of the same audit ─────────────────
def test_epochs_are_still_spelled_out_for_the_judge():
    assert "2026-08-11T00:00:00Z" in annotate_epochs({"start_time": 1786406400})["start_time"]
