I want a volume-aware Bollinger breakout strategy on BTC perps on
Hyperliquid, hourly candles. The idea is that a breakout only counts when
volume flow agrees with it. Use 20-period Bollinger Bands at 2 standard
deviations plus the accumulation/distribution line: go long $1,000 when
price closes above the upper band AND the A/D line is higher than it was 10
candles ago AND the current candle's volume is at least 1.5x its 20-period
average. Go short $1,000 on the mirror image (close below the lower band,
A/D falling, same volume confirmation). One position at a time, take profit
at +6%, stop loss at −3%.
