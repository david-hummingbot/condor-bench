Build a VWAP scalping strategy on BTC perps on Hyperliquid, hourly candles.
Compute a rolling 48-hour volume-weighted average price (use typical price =
(high + low + close) / 3) and put bands at 1.5 standard deviations of the
close around it. When price closes below the lower band, go long $1,000;
when it closes above the upper band, go short $1,000. Exit each trade when
price comes back to the VWAP. Add a 2% stop loss on every position.
