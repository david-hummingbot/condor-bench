"""Reference: RSIStarter (WolfBot) — oversold long entries with a LOSS
COOLDOWN (stateful: ctx.stopped + ctx.memo).

Two entry modes: immediately on deep oversold (RSI <= low_start_factor *
low), or on oversold RECOVERY (RSI crosses back up through low). After any
losing round trip, pause pause_candles before trading again. Long only,
TP/SL barriers.
"""

import numpy as np

from condor.backtest.types import Create
from condor.executors.position import PositionPerpConfig

PARAMS = {
    "coin": "BTC",
    "notional_quote": 1000.0,
    "rsi_period": 14,
    "low": 30.0,
    "low_start_factor": 0.75,
    "pause_candles": 20,
    "take_profit_pct": 0.05,
    "stop_loss_pct": 0.025,
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
    if len(close) < p["rsi_period"] + 3:
        return []
    step = float(candles.index[-1] - candles.index[-2])

    # Loss cooldown: any losing closed round trip pushes the pause window out.
    pause_until = ctx.memo.get("pause_until_ts", 0.0)
    for s in ctx.stopped:
        if s.close_ts is not None and s.net_pnl_quote < 0:
            pause_until = max(pause_until, s.close_ts + p["pause_candles"] * step)
    ctx.memo["pause_until_ts"] = pause_until
    if ctx.ts < pause_until or ctx.active:
        return []

    rsi = wilder_rsi(close, p["rsi_period"])
    rsi_now, rsi_prev = float(rsi.iloc[-1]), float(rsi.iloc[-2])
    deep = rsi_now <= p["low_start_factor"] * p["low"]
    recovery = rsi_prev < p["low"] and rsi_now >= p["low"]
    if not (deep or recovery):
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
            tag="deep_oversold" if deep else "oversold_recovery",
        )
    ]
