"""Reference: SMA crossover, long-only (botcamp "Buy Low & Sell high").

Long on the golden cross (SMA10 over SMA30), close on the death cross.
"""

from condor.backtest.types import Create, Stop
from condor.executors.position import PositionPerpConfig

PARAMS = {"coin": "BTC", "notional_quote": 1000.0, "fast": 10, "slow": 30}


def decide(candles, ctx):
    p = {**PARAMS, **ctx.params}
    close = candles["close"]
    if len(close) < p["slow"] + 2:
        return []
    fast = close.rolling(p["fast"]).mean()
    slow = close.rolling(p["slow"]).mean()
    golden = fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]
    death = fast.iloc[-2] >= slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]
    if ctx.active and death:
        return [Stop(a.id) for a in ctx.active]
    if not ctx.active and golden:
        return [
            Create(
                PositionPerpConfig(
                    coin=p["coin"], side="LONG", notional_quote=p["notional_quote"]
                ),
                tag="golden_cross",
            )
        ]
    return []
