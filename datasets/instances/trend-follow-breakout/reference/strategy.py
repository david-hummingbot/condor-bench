"""Reference: perp trend-following (botcamp "Directional Strategy on
WLD-USDT Perp", genericized to the fixture coin) — ride the wave.

Donchian(55) breakout: close above the prior 55-candle high -> LONG, below
the prior low -> SHORT. Ride with a trailing stop (arm at +2%, trail 1%);
an opposite breakout flips the position.
"""

from condor.backtest.types import Create, Stop
from condor.executors.position import PositionPerpConfig

PARAMS = {
    "coin": "BTC",
    "notional_quote": 1000.0,
    "channel": 55,
    "trailing_activation_pct": 0.02,
    "trailing_delta_pct": 0.01,
}


def decide(candles, ctx):
    p = {**PARAMS, **ctx.params}
    close = candles["close"]
    n = p["channel"]
    if len(close) < n + 2:
        return []
    price = float(close.iloc[-1])
    prior_high = float(candles["high"].iloc[-(n + 1):-1].max())
    prior_low = float(candles["low"].iloc[-(n + 1):-1].min())
    breakout_up = price > prior_high
    breakout_dn = price < prior_low
    if not (breakout_up or breakout_dn):
        return []
    want = "LONG" if breakout_up else "SHORT"

    actions = [Stop(a.id) for a in ctx.active if a.side != want]
    if not any(a.side == want for a in ctx.active):
        actions.append(
            Create(
                PositionPerpConfig(
                    coin=p["coin"],
                    side=want,
                    notional_quote=p["notional_quote"],
                    trailing_activation_pct=p["trailing_activation_pct"],
                    trailing_delta_pct=p["trailing_delta_pct"],
                ),
                tag="breakout",
            )
        )
    return actions
