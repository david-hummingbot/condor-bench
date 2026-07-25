"""Reference: PriceRangeTrader (WolfBot) — sideways-range ping-pong with
RESTING LIMIT ORDERS.

When flat and the recent range is wide enough, rest a limit buy at the range
low; once it fills (position held), rest a limit sell at breakeven + profit.
Resting orders that sit unfilled for order_ttl_candles are cancelled and
re-evaluated. Exercises order executors, the netting ledger, and cancels.
"""

from condor.backtest.types import Create, Stop
from condor.executors.order import OrderPerpConfig

PARAMS = {
    "coin": "BTC",
    "notional_quote": 500.0,
    "range_window": 24,
    "min_range_pct": 0.02,
    "profit_pct": 0.02,
    "order_ttl_candles": 12,
}


def decide(candles, ctx):
    p = {**PARAMS, **ctx.params}
    if len(candles) < p["range_window"] + 2:
        return []
    step = float(candles.index[-1] - candles.index[-2])
    actions = []

    # Cancel resting orders that outlived their TTL.
    resting = [a for a in ctx.active if not a.is_filled]
    expired = {
        a.id for a in resting if ctx.ts - a.created_ts >= p["order_ttl_candles"] * step
    }
    actions.extend(Stop(eid) for eid in sorted(expired))
    live = [a for a in resting if a.id not in expired]
    resting_buys = [a for a in live if a.side == "LONG"]
    resting_sells = [a for a in live if a.side == "SHORT"]
    held = [pos for pos in ctx.positions if pos.side == "LONG" and pos.amount_base > 0]

    if held:
        if not resting_sells:
            pos = held[0]
            sell_px = pos.breakeven_price * (1.0 + p["profit_pct"])
            actions.append(
                Create(
                    OrderPerpConfig(
                        coin=p["coin"],
                        side="SHORT",
                        notional_quote=pos.amount_base * sell_px,
                        order_type="limit",
                        limit_px=sell_px,
                    ),
                    tag="range_sell",
                )
            )
    elif not resting_buys:
        low = float(candles["low"].iloc[-p["range_window"]:].min())
        high = float(candles["high"].iloc[-p["range_window"]:].max())
        if (high - low) / low >= p["min_range_pct"]:
            actions.append(
                Create(
                    OrderPerpConfig(
                        coin=p["coin"],
                        side="LONG",
                        notional_quote=p["notional_quote"],
                        order_type="limit",
                        limit_px=low,
                    ),
                    tag="range_buy",
                )
            )
    return actions
