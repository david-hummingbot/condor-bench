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
