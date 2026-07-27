"""Reference: Asymmetric Barbell (botcamp) — MACD + Bollinger confluence with
asymmetric sizing: full-size longs, one-third-size shorts (the barbell).

MACD(12,26,9) cross up while price is at/below the BB mid-band -> LONG;
cross down while at/above the mid-band -> SHORT. 5% TP / 2.5% SL barriers.
"""

from condor.backtest.types import Create
from condor.executors.position import PositionPerpConfig

PARAMS = {
    "coin": "BTC",
    "long_notional": 1500.0,
    "short_notional": 500.0,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "bb_period": 20,
    "take_profit_pct": 0.05,
    "stop_loss_pct": 0.025,
}


def decide(candles, ctx):
    p = {**PARAMS, **ctx.params}
    close = candles["close"]
    if len(close) < p["macd_slow"] + p["macd_signal"] + 2:
        return []
    ema_fast = close.ewm(span=p["macd_fast"], adjust=False).mean()
    ema_slow = close.ewm(span=p["macd_slow"], adjust=False).mean()
    macd = ema_fast - ema_slow
    signal = macd.ewm(span=p["macd_signal"], adjust=False).mean()
    cross_up = macd.iloc[-2] <= signal.iloc[-2] and macd.iloc[-1] > signal.iloc[-1]
    cross_dn = macd.iloc[-2] >= signal.iloc[-2] and macd.iloc[-1] < signal.iloc[-1]
    if not (cross_up or cross_dn) or ctx.active:
        return []

    mid = float(close.rolling(p["bb_period"]).mean().iloc[-1])
    price = float(close.iloc[-1])
    if cross_up and price <= mid:
        side, notional = "LONG", p["long_notional"]
    elif cross_dn and price >= mid:
        side, notional = "SHORT", p["short_notional"]
    else:
        return []
    return [
        Create(
            PositionPerpConfig(
                coin=p["coin"],
                side=side,
                notional_quote=notional,
                take_profit_pct=p["take_profit_pct"],
                stop_loss_pct=p["stop_loss_pct"],
            ),
            tag="barbell",
        )
    ]
