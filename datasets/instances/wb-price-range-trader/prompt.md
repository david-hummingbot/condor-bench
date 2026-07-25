I want a range ping-pong strategy for sideways BTC markets on Hyperliquid
perps, hourly candles, using resting limit orders (I want to earn maker
fills, not cross the spread). When I'm flat: look at the last 24 candles,
and if the range between the lowest low and highest high is at least 2% of
the low, rest a $500 limit buy at that range low. Once the buy fills and I'm
holding the position, rest a limit sell of the whole position at my entry
price plus 2%. If any resting order sits unfilled for 12 hours, cancel it
and re-evaluate. Repeat forever — buy the bottom of the range, sell 2%
higher.
