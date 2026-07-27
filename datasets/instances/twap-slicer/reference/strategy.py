"""Reference: TWAP slicer (botcamp "Simple VWAP" — buy a large amount
without moving the price).

Accumulate $5,000 of BTC in 10 equal market slices, one every 4 candles.
Slices are order executors: each fill hands its position to the ledger.
"""

from condor.backtest.types import Create
from condor.executors.order import OrderPerpConfig

PARAMS = {
    "coin": "BTC",
    "total_notional": 5000.0,
    "slices": 10,
    "every_candles": 4,
}


def decide(candles, ctx):
    p = {**PARAMS, **ctx.params}
    placed = ctx.memo.get("twap_placed", 0)
    if placed >= p["slices"]:
        return []
    last_i = ctx.memo.get("twap_last_i")
    if last_i is not None and ctx.i - last_i < p["every_candles"]:
        return []
    ctx.memo["twap_placed"] = placed + 1
    ctx.memo["twap_last_i"] = ctx.i
    return [
        Create(
            OrderPerpConfig(
                coin=p["coin"],
                side="LONG",
                notional_quote=p["total_notional"] / p["slices"],
            ),
            tag=f"slice_{placed + 1}",
        )
    ]
