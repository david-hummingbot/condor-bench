"""Reference: VWAP Scalper (botcamp) — rolling VWAP with volatility bands.

Fade closes outside vwap ± k·std back to the vwap; 2% stop guard.
"""

from condor.backtest.types import Create, Stop
from condor.executors.position import PositionPerpConfig

PARAMS = {
    "coin": "BTC",
    "notional_quote": 1000.0,
    "vwap_window": 48,
    "band_k": 1.5,
    "stop_loss_pct": 0.02,
}


def decide(candles, ctx):
    p = {**PARAMS, **ctx.params}
    if len(candles) < p["vwap_window"] + 2:
        return []
    w = candles.iloc[-p["vwap_window"]:]
    typical = (w["high"] + w["low"] + w["close"]) / 3.0
    vol = w["volume"].clip(lower=1e-12)
    vwap = float((typical * vol).sum() / vol.sum())
    band = p["band_k"] * float(w["close"].std())
    price = float(candles["close"].iloc[-1])

    if ctx.active:
        stops = []
        for a in ctx.active:
            if a.side == "LONG" and price >= vwap:
                stops.append(Stop(a.id))
            elif a.side == "SHORT" and price <= vwap:
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
                tag="vwap_fade",
            )
        ]

    if price < vwap - band:
        return entry("LONG")
    if price > vwap + band:
        return entry("SHORT")
    return []
