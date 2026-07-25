"""Reference: VWAD Enhanced Bollinger (botcamp) — volume-confirmed Bollinger
breakouts filtered by the accumulation/distribution line's direction.

Close above BB(20,2) upper + A/D rising + volume > 1.5x its 20-SMA -> LONG;
mirror below the lower band with A/D falling -> SHORT. 6% TP / 3% SL.
"""

from condor.backtest.types import Create
from condor.executors.position import PositionPerpConfig

PARAMS = {
    "coin": "BTC",
    "notional_quote": 1000.0,
    "bb_period": 20,
    "bb_std": 2.0,
    "ad_lookback": 10,
    "volume_mult": 1.5,
    "take_profit_pct": 0.06,
    "stop_loss_pct": 0.03,
}


def decide(candles, ctx):
    p = {**PARAMS, **ctx.params}
    if len(candles) < p["bb_period"] + p["ad_lookback"] + 2 or ctx.active:
        return []
    close, high, low, vol = (
        candles["close"],
        candles["high"],
        candles["low"],
        candles["volume"],
    )
    mid = close.rolling(p["bb_period"]).mean()
    std = close.rolling(p["bb_period"]).std()
    upper = float(mid.iloc[-1] + p["bb_std"] * std.iloc[-1])
    lower = float(mid.iloc[-1] - p["bb_std"] * std.iloc[-1])
    price = float(close.iloc[-1])

    # Accumulation/Distribution: money-flow multiplier x volume, cumulative.
    span = (high - low).replace(0.0, float("nan"))
    mfm = (((close - low) - (high - close)) / span).fillna(0.0)
    ad = (mfm * vol).cumsum()
    ad_rising = float(ad.iloc[-1] - ad.iloc[-1 - p["ad_lookback"]]) > 0
    vol_confirmed = float(vol.iloc[-1]) > p["volume_mult"] * float(
        vol.rolling(p["bb_period"]).mean().iloc[-1]
    )
    if not vol_confirmed:
        return []

    if price > upper and ad_rising:
        side = "LONG"
    elif price < lower and not ad_rising:
        side = "SHORT"
    else:
        return []
    return [
        Create(
            PositionPerpConfig(
                coin=p["coin"],
                side=side,
                notional_quote=p["notional_quote"],
                take_profit_pct=p["take_profit_pct"],
                stop_loss_pct=p["stop_loss_pct"],
            ),
            tag="vwad_breakout",
        )
    ]
