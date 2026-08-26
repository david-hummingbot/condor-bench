"""Tests for judge-facing tool-output digests."""
from __future__ import annotations

from bench.client import BenchmarkResult, TurnResult
from bench.tool_digest import digest_tool_output


def _live_portfolio_blob(*, dust_n: int = 230) -> str:
    """Shape matching hummingbot-api's formatted get_portfolio_overview output."""
    rows = [
        "token    | connector         | total        | available    | value_usd",
        "-" * 80,
        "USDT     | bitget            | 3013.6       | 3013.6       | 3013.60",
    ]
    for i in range(dust_n):
        rows.append(
            f"DUST{i:03d}  | bitget            | {i * 0.001:<12.4g} | {i * 0.001:<12.4g} | 0.00"
        )
    rows.extend(
        [
            "XRP      | xrpl              | 142.51       | 138.9        | 146.74",
            "RLUSD    | xrpl              | 90.88        | 90.88        | 90.88",
            "USDC     | bitget            | 0.33         | 0.33         | 0.33",
        ]
    )
    return "\n".join(
        [
            "Portfolio Overview",
            "=" * 80,
            "",
            "💰 Token Balances:",
            "-" * 80,
            *rows,
            "",
            "📈 Summary:",
            "-" * 80,
            "Total Balance Value: $3251.55",
            "Active Perpetual Positions: 0",
            "Active LP Positions: 0",
            "Active Orders: 0",
        ]
    )


def test_portfolio_digest_keeps_totals_and_valued_holdings():
    blob = _live_portfolio_blob()
    # Old head-truncate could not see XRPL / total.
    assert "XRP" not in blob[:700]
    assert "3251" not in blob[:700]

    digest = digest_tool_output("get_portfolio_overview", blob)
    assert "Total Balance Value: $3251.55" in digest
    assert "XRP" in digest and "146.74" in digest
    assert "RLUSD" in digest and "90.88" in digest
    assert "USDT" in digest and "3013.6" in digest
    assert "USDC" in digest
    assert "zero/unpriced omitted" in digest
    # Dust tokens must not dominate the digest.
    assert digest.count("DUST") == 0


def test_mock_json_portfolio_digest():
    payload = {
        "total_balance_usd": 12450.32,
        "available_balance_usd": 8320.1,
        "positions": [],
        "open_orders": [{"order_id": "ord-1"}],
    }
    digest = digest_tool_output("get_portfolio_overview", payload)
    assert "12450.32" in digest
    assert "8320.1" in digest
    assert "positions: [] (0)" in digest
    assert "open_orders: 1 items" in digest


def test_short_output_passes_through():
    text = "BTC-USDT mid_price=65000.0"
    assert digest_tool_output("get_market_data", text) == text


def test_transcript_for_judge_surfaces_portfolio_facts():
    blob = _live_portfolio_blob()
    result = BenchmarkResult(
        case_id="c001",
        model="m",
        turns=[
            TurnResult(
                response="Total portfolio value: ~$3,251.55",
                tool_calls=[
                    {
                        "tool": "get_portfolio_overview",
                        "args": {"refresh": True},
                        "tool_call_id": "t1",
                    }
                ],
                latency_s=1.0,
                tool_responses=[
                    {
                        "tool": "get_portfolio_overview",
                        "output": blob,
                        "tool_call_id": "t1",
                    }
                ],
            )
        ],
    )

    text = result.transcript_for_judge()
    assert "get_portfolio_overview" in text
    assert "Total Balance Value: $3251.55" in text
    assert "XRP" in text and "RLUSD" in text
    assert "3,251.55" in text or "3251.55" in text


def test_transcript_for_judge_includes_tools():
    result = BenchmarkResult(
        case_id="x",
        model="m",
        turns=[
            TurnResult("first", [{"tool": "get_market_data", "args": {}}], 1.0),
            TurnResult("second", [{"tool": "manage_executors", "args": {}}], 1.0),
        ],
    )
    text = result.transcript_for_judge()
    assert "Turn 1" in text and "Turn 2" in text
    assert "get_market_data" in text and "manage_executors" in text


def test_json_list_digest_summarises_length():
    candles = [{"o": i, "h": i, "l": i, "c": i} for i in range(500)]
    digest = digest_tool_output("get_market_data", candles)
    assert "500 items" in digest
    # A lossy digest now says so, and the note is part of the digest. Measure the
    # body against the original bound — "summarised, not dumped" is the claim here.
    body = digest.split("\n[digest truncated:")[0]
    assert len(body) < 500
    assert "[digest truncated:" in digest, "dropping 500 candles must be declared"


# ── the answer must reach the judge ────────────────────────────────────────────
def test_the_answer_survives_a_tool_log_longer_than_the_judge_window():
    """The failure this ordering exists to prevent.

    The judge is shown only the first JUDGE_INPUT_CHARS of the transcript. With the
    tool log first, eight calls at the digest budget produced ~13k characters and
    pushed the answer past the cap — the judge reported "cuts off before any actual
    response" and scored a complete, correct answer 0.15.
    """
    from bench.client import BenchmarkResult, TurnResult
    from metrics.answer_quality import JUDGE_INPUT_CHARS

    calls = [
        {"tool": f"manage_x{i}", "args": {"action": "list"}, "tool_call_id": str(i)}
        for i in range(8)
    ]
    responses = [
        {"tool": f"manage_x{i}", "tool_call_id": str(i), "output": "y" * 4000}
        for i in range(8)
    ]
    result = BenchmarkResult(
        case_id="x",
        model="m",
        turns=[
            TurnResult(
                response="THE ACTUAL ANSWER",
                tool_calls=calls,
                latency_s=1.0,
                tool_responses=responses,
            )
        ],
    )
    # Eight calls at the full per-tool allowance used to produce ~13k characters against
    # an 8000 cap, so a third of the log was unreachable however it was ordered — and
    # the tail is where the *last* tool's output lives. A judge shown a truncated log
    # concluded a call had never happened and marked a verbatim quote as fabricated
    # (`agent_directional_trader_002`, aq 0.55). The log budget is now shared across the
    # calls, so the whole transcript fits.
    transcript = result.transcript_for_judge()
    assert len(transcript) <= JUDGE_INPUT_CHARS, (
        f"tool log budget exceeded the judge window: {len(transcript)} chars"
    )
    assert "THE ACTUAL ANSWER" in transcript[:JUDGE_INPUT_CHARS], (
        "the answer was pushed out of the judge's view by the tool log"
    )
    # Every call is named within the window, so "it never called X" is not a conclusion
    # truncation can produce.
    for i in range(8):
        assert f"manage_x{i}" in transcript[:JUDGE_INPUT_CHARS], f"call {i} unnamed"

    # And if a caller overrides the budget back up, answer-first ordering still protects
    # the answer — the property this test was originally written for.
    overflowing = result.transcript_for_judge(output_chars=4000)
    assert len(overflowing) > JUDGE_INPUT_CHARS
    assert "THE ACTUAL ANSWER" in overflowing[:JUDGE_INPUT_CHARS]


def test_tool_log_still_reaches_the_judge_for_short_transcripts():
    """Reordering must not cost the grounding check on ordinary cases."""
    from bench.client import BenchmarkResult, TurnResult
    from metrics.answer_quality import JUDGE_INPUT_CHARS

    result = BenchmarkResult(
        case_id="x",
        model="m",
        turns=[
            TurnResult(
                response="mid price is 65000",
                tool_calls=[{"tool": "get_market_data", "args": {}, "tool_call_id": "1"}],
                latency_s=1.0,
                tool_responses=[
                    {"tool": "get_market_data", "tool_call_id": "1", "output": '{"mid_price": 65000}'}
                ],
            )
        ],
    )
    visible = result.transcript_for_judge()[:JUDGE_INPUT_CHARS]
    assert "mid price is 65000" in visible
    assert "get_market_data" in visible
    assert "65000" in visible


# ── A payload string is the answer, not a scalar to preview ───────────────────
# hummingbot's tools wrap their formatted output in {"result": "<text>"}. The
# structured digester previewed string fields at 200 chars, which cut an order book
# off at its column headers — one line before the first price. The judge was shown a
# table with no rows, and said the cited bid/ask "appear fabricated rather than
# grounded in visible tool output". It was right: they were not visible.
ORDER_BOOK = {
    "result": (
        "Order Book Snapshot for BTC-USDT on binance:\n"
        "Timestamp: 2026-08-12 08:41:12\n"
        "Top 10 Levels:\n\n"
        "BIDS                      |  ASKS\n"
        "price      | amount       |  price      | amount\n"
        "------------------------------------------------\n"
        "63719.99   | 0.5421       |  63720.00   | 1.2033\n"
        "63719.50   | 1.1002       |  63720.45   | 0.8890\n"
        "63719.10   | 2.4410       |  63721.00   | 3.1120\n"
    )
}


def test_the_figures_an_answer_would_cite_survive_the_digest():
    from bench.tool_digest import digest_tool_output

    digest = digest_tool_output("get_market_data", ORDER_BOOK)
    assert "63719.99" in digest, digest
    assert "63720.00" in digest, digest


def test_a_short_payload_is_passed_through_verbatim():
    """No summary beats the real thing when the real thing fits."""
    from bench.tool_digest import digest_tool_output

    payload = {"result": "Active Bots Status Summary:\nTotal Active Bots: 0"}
    digest = digest_tool_output("manage_bots", payload)
    assert "Total Active Bots: 0" in digest


def test_a_long_payload_is_summarised_within_budget():
    from bench.tool_digest import DEFAULT_DIGEST_CHARS, digest_tool_output

    rows = "".join(f"row {i} | amount {i * 7}\n" for i in range(400))
    digest = digest_tool_output("search_history", {"result": rows})
    assert len(digest) <= DEFAULT_DIGEST_CHARS
    assert "399 data rows" in digest, "the reader must be told what was dropped"


def test_short_string_fields_are_still_previewed_inline():
    from bench.tool_digest import digest_tool_output

    digest = digest_tool_output("manage_servers", {"server": "bench_staging", "status": "online"})
    assert "bench_staging" in digest
    assert "online" in digest


def test_several_payload_fields_share_the_budget():
    """One long field must not crowd the others out entirely."""
    from bench.tool_digest import DEFAULT_DIGEST_CHARS, digest_tool_output

    payload = {"bids": "b" * 3000, "asks": "a" * 3000, "pair": "BTC-USDT"}
    digest = digest_tool_output("get_market_data", payload)
    assert len(digest) <= DEFAULT_DIGEST_CHARS
    assert "bids" in digest and "asks" in digest and "BTC-USDT" in digest


# ── The portfolio digester had stopped running ────────────────────────────────
# It reads the rendered table out of the payload, but only knew the key
# `formatted_output`. hummingbot answers with `result`, so on every live call it
# parsed the JSON envelope, found no holdings and no total, and returned nothing —
# the one digester written for the dust-row problem was dead for the shape it was
# written against.
_PORTFOLIO_HEADER = (
    "Portfolio Overview\n" + "=" * 60 + "\n\n💰 Token Balances:\n"
    "token | connector | units | available | value_usd\n" + "-" * 48 + "\n"
)


def test_the_portfolio_digester_reads_the_result_envelope():
    from bench.tool_digest import digest_tool_output

    payload = {
        "result": _PORTFOLIO_HEADER
        + "USDT  | binance | 1000 | 1000 | 1000.00\n"
        + "BTC   | binance | 0.5  | 0.5  | 31800.00\n\n"
        + "Total Balance Value: $32,800.00\n"
    }
    digest = digest_tool_output("get_portfolio_overview", payload)
    assert digest.startswith("[digest] portfolio"), digest
    assert "32,800.00" in digest
    assert "31800.00" in digest


def test_dust_rows_cannot_push_the_total_out_of_view():
    """The failure this digester exists to prevent."""
    from bench.tool_digest import DEFAULT_DIGEST_CHARS, digest_tool_output

    dust = "".join(f"JUNK{i} | binance | 0.00001 | 0 | 0.00\n" for i in range(300))
    payload = {
        "result": _PORTFOLIO_HEADER
        + "BTC | binance | 0.5 | 0.5 | 31800.00\n"
        + dust
        + "\nTotal Balance Value: $32,800.00\n"
    }
    digest = digest_tool_output("get_portfolio_overview", payload)
    assert len(digest) <= DEFAULT_DIGEST_CHARS
    assert "32,800.00" in digest, "the total an answer would cite"
    assert "31800.00" in digest, "the holding an answer would cite"
    assert "300 zero/unpriced omitted" in digest, "say what was dropped"


# ── Nested list fields on each row must stay citeable ─────────────────────────
# list_agent_definitions returns strategies: ["BTC-USDT Adaptive Grid", …] per
# agent. The row renderer kept only scalars, so the judge never saw those names
# and marked a verbatim Strategies column as fabricated (c011 aq 0.35,
# tool_manage_trading_agent_001 aq 0.55).
_AGENT_DEFINITIONS = {
    "agents": [
        {
            "slug": "adaptive_grid_trader",
            "name": "Adaptive Grid Trader",
            "description": (
                "Expert in multi-timeframe adaptive grid trading with safety-first "
                "order sizing, a configurable untraded reserve, and strategy controls."
            ),
            "agent_key": "anthropic:claude-sonnet-4-5",
            "when_to_consult": "when you want adaptive grid trading on BTC or SOL",
            "strategies": ["BTC-USDT Adaptive Grid", "SOL-USDT Adaptive Grid"],
            "tools": ["manage_executors", "get_market_data"],
        },
        {
            "slug": "directional_trader",
            "name": "Directional Trader",
            "description": "Signal engineering, controllers, and backtesting.",
            "agent_key": "anthropic:claude-sonnet-4-5",
            "when_to_consult": "trend and signal work",
            "strategies": ["EMA Trend Loop"],
            "tools": [],
        },
    ]
}


def test_agent_definition_strategies_reach_the_judge():
    """The failure this nested-list rendering exists to prevent."""
    digest = digest_tool_output("manage_trading_agent", _AGENT_DEFINITIONS)
    assert "BTC-USDT Adaptive Grid" in digest
    assert "SOL-USDT Adaptive Grid" in digest
    assert "EMA Trend Loop" in digest
    assert "Adaptive Grid Trader" in digest
    assert "Directional Trader" in digest


def test_empty_nested_lists_are_still_shown():
    """Empty strategies=[] must be visible so invented names stay falsifiable."""
    payload = {
        "agents": [
            {
                "slug": "condor",
                "name": "Condor",
                "description": "General assistant",
                "strategies": [],
                "tools": [],
            }
        ]
    }
    digest = digest_tool_output("manage_trading_agent", payload)
    assert "strategies=[]" in digest


# ── a lossy digest must say it is lossy ───────────────────────────────────────
def test_a_truncated_digest_declares_what_it_dropped():
    """`tool_manage_skill_001` scored 0.15 for a verifiably correct answer.

    It read `routine_cookbook/report_builder.md` — 10,135 characters — and
    summarised RoutineResult accurately: the `routines.base` import path,
    `table_data`/`table_columns`, `chart_image`, and the
    `{"type": "kpi", …, "trend": "up"|"down"}` section shape are all on lines
    190-211 of the real file. The judge's share of the tool log was ~1,375
    characters, roughly 13% of it, and that section sits near the end. Seeing none
    of it, the judge wrote that the field descriptions "do not appear anywhere in
    the report_builder.md digest" and scored it as fabrication.

    The judge cannot be right about absence unless it knows the digest is partial.
    """
    from bench.tool_digest import digest_tool_output

    long_doc = "\n".join(
        f"## Section {i}\nProse about the RoutineResult return type and its fields."
        for i in range(200)
    )
    digest = digest_tool_output("manage_skill", long_doc, max_chars=600)
    assert "[digest truncated:" in digest
    assert str(len(long_doc)) in digest, "the judge needs the scale of what it lost"
    assert len(digest) <= 600, "the note is paid for from the budget, not added to it"


def test_a_complete_output_is_left_unmarked():
    """The marker is only worth anything if silence means complete.

    `tool_get_user_context_001` claimed two saved memories when `manage_memory`
    had returned `{"index": ""}` in full — 17 characters, nothing dropped. That
    accusation was correct, and a blanket "the log may be incomplete" caveat would
    have excused it. An unmarked output must stay unmarked so the judge can still
    convict on one.
    """
    from bench.tool_digest import digest_tool_output

    assert "[digest truncated:" not in digest_tool_output("manage_memory", {"index": ""})
    assert "[digest truncated:" not in digest_tool_output("x", "short and complete")


def test_the_judge_is_told_how_to_read_an_absence():
    """Absence from a truncated digest is not evidence; contradiction always is."""
    from metrics.answer_quality import _CRITERIA

    assert "DIGESTED" in _CRITERIA
    assert "digest truncated" in _CRITERIA
    assert "CONTRADICTS" in _CRITERIA
