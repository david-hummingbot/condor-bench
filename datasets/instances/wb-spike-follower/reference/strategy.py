"""Reference: PriceSpikeDetector (WolfBot) — trade WITH a price spike when
the daily trend agrees.

A single candle moving >= spike_pct, confirmed against the level
history_candle candles back and aligned with the trend_window trend, opens a
position in the spike direction with TP/SL barriers. One position at a time.
"""

from condor.backtest.types import Create
from condor.executors.position import PositionPerpConfig

PARAMS = {
    "coin": "BTC",
    "notional_quote": 1000.0,
    "spike_pct": 0.02,
    "history_candle": 36,
    "trend_window": 24,
    "take_profit_pct": 0.04,
    "stop_loss_pct": 0.02,
}


def decide(candles, ctx):
    p = {**PARAMS, **ctx.params}
    close = candles["close"]
    need = max(p["history_candle"], p["trend_window"]) + 2
    if len(close) < need or ctx.active:
        return []

    change = float(close.iloc[-1] / close.iloc[-2] - 1.0)
    if abs(change) < p["spike_pct"]:
        return []
    trend = float(close.iloc[-1] / close.iloc[-(p["trend_window"] + 1)] - 1.0)
    history_move = float(close.iloc[-1] / close.iloc[-(p["history_candle"] + 1)] - 1.0)
    # Spike, daily trend, and the longer history must all point the same way.
    if not (change > 0 and trend > 0 and history_move > 0) and not (
        change < 0 and trend < 0 and history_move < 0
    ):
        return []

    side = "LONG" if change > 0 else "SHORT"
    return [
        Create(
            PositionPerpConfig(
                coin=p["coin"],
                side=side,
                notional_quote=p["notional_quote"],
                take_profit_pct=p["take_profit_pct"],
                stop_loss_pct=p["stop_loss_pct"],
            ),
            tag="spike",
        )
    ]
