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
    assert len(digest) < 500


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
    transcript = result.transcript_for_judge()
    assert len(transcript) > JUDGE_INPUT_CHARS, (
        "premise gone: this transcript no longer overflows the judge window"
    )
    assert "THE ACTUAL ANSWER" in transcript[:JUDGE_INPUT_CHARS], (
        "the answer was pushed out of the judge's view by the tool log"
    )


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
