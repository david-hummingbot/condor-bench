# Suite templates

A comparison of two Condor checkouts should exercise what users actually ask for. These are loaded into a compare suite by the dashboard's Compare Condor page; edit or extend them here. Same case format as datasets/*.jsonl.

`practical.jsonl` — 14 real-world tasks: balance, portfolio, price, candles, running executors, trade history, bots, DEX pools, a limit buy, a long with stops, leverage, a balance-sized buy, closing out a pair, and a reckless request that should be confirmed rather than executed.

Cases that place orders assert a RUNNING executor afterwards (post_conditions) and are torn down. They run on the live lane against staging — the same rules as the library.
