"""Reference: Chop Cutter (botcamp) — Bollinger mean reversion for sideways
markets. Long at the lower band, short at the upper, exit at the mid-band,
3% stop guard against a real trend.
"""

from condor.backtest.types import Create, Stop
from condor.executors.position import PositionPerpConfig

PARAMS = {
    "coin": "BTC",
    "notional_quote": 1000.0,
    "bb_period": 20,
    "bb_std": 2.0,
    "stop_loss_pct": 0.03,
}


def decide(candles, ctx):
    p = {**PARAMS, **ctx.params}
    close = candles["close"]
    if len(close) < p["bb_period"] + 2:
        return []
    mid = close.rolling(p["bb_period"]).mean()
    std = close.rolling(p["bb_period"]).std()
    upper = float(mid.iloc[-1] + p["bb_std"] * std.iloc[-1])
    lower = float(mid.iloc[-1] - p["bb_std"] * std.iloc[-1])
    mid_now = float(mid.iloc[-1])
    price = float(close.iloc[-1])

    if ctx.active:
        # Mean reached: a long exits at/above the mid-band, a short at/below.
        stops = []
        for a in ctx.active:
            if a.side == "LONG" and price >= mid_now:
                stops.append(Stop(a.id))
            elif a.side == "SHORT" and price <= mid_now:
                stops.append(Stop(a.id))
        return stops

    def entry(side):
        return [
            Create(
                PositionPerpConfig(
                    coin=p["coin"],
                    side=side,
                    notional_quote=p["notional_quote"],
                    stop_loss_pct=p["stop_loss_pct"],
                ),
                tag="band_fade",
            )
        ]

    if price < lower:
        return entry("LONG")
    if price > upper:
        return entry("SHORT")
    return []
