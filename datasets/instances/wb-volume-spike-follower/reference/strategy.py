"""Reference: VolumeSpikeDetector (WolfBot) — volume-spike momentum, long
only, trend-aligned.

A candle whose volume is >= spike_factor x the average of the previous
vol_window candles, with an up candle body of at least min_move_pct and the
trend_window trend up, opens a long with TP/SL barriers.
"""

from condor.backtest.types import Create
from condor.executors.position import PositionPerpConfig

PARAMS = {
    "coin": "BTC",
    "notional_quote": 1000.0,
    "spike_factor": 3.0,
    "vol_window": 24,
    "min_move_pct": 0.01,
    "trend_window": 24,
    "take_profit_pct": 0.04,
    "stop_loss_pct": 0.02,
}


def decide(candles, ctx):
    p = {**PARAMS, **ctx.params}
    need = max(p["vol_window"], p["trend_window"]) + 2
    if len(candles) < need or ctx.active:
        return []
    vol = candles["volume"]
    avg_vol = float(vol.iloc[-(p["vol_window"] + 1):-1].mean())
    if avg_vol <= 0 or float(vol.iloc[-1]) < p["spike_factor"] * avg_vol:
        return []
    body = float(candles["close"].iloc[-1] / candles["open"].iloc[-1] - 1.0)
    if body < p["min_move_pct"]:
        return []
    close = candles["close"]
    trend = float(close.iloc[-1] / close.iloc[-(p["trend_window"] + 1)] - 1.0)
    if trend <= 0:
        return []
    return [
        Create(
            PositionPerpConfig(
                coin=p["coin"],
                side="LONG",
                notional_quote=p["notional_quote"],
                take_profit_pct=p["take_profit_pct"],
                stop_loss_pct=p["stop_loss_pct"],
            ),
            tag="volume_spike",
        )
    ]
