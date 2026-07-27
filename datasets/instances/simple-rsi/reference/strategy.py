"""Reference: Simple RSI (botcamp "Simple RSI").

Long $notional when hourly RSI(14) < 30; close the position when RSI > 70.
"""

import numpy as np

from condor.backtest.types import Create, Stop
from condor.executors.position import PositionPerpConfig

PARAMS = {
    "coin": "BTC",
    "notional_quote": 1000.0,
    "rsi_period": 14,
    "buy_below": 30.0,
    "sell_above": 70.0,
}


def wilder_rsi(close, period):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0.0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def decide(candles, ctx):
    p = {**PARAMS, **ctx.params}
    close = candles["close"]
    if len(close) < p["rsi_period"] + 2:
        return []
    rsi = float(wilder_rsi(close, p["rsi_period"]).iloc[-1])
    if ctx.active:
        if rsi > p["sell_above"]:
            return [Stop(a.id) for a in ctx.active]
        return []
    if rsi < p["buy_below"]:
        return [
            Create(
                PositionPerpConfig(
                    coin=p["coin"], side="LONG", notional_quote=p["notional_quote"]
                ),
                tag="rsi_dip",
            )
        ]
    return []
