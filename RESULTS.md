Trading Results & Strategy Analysis
Project Summary
This document summarizes the development, live trading results, and analytical findings from an autonomous algorithmic trading bot built for Kalshi's weather temperature prediction markets. The project ran from June 2026 through August 2026, progressing through two distinct strategies, live trading with real money, and multiple rounds of data-driven strategy refinement.
---
Timeline
Date	Milestone
June 2026	Bot infrastructure built: API auth, Docker deployment, SQLite, Telegram
Late June 2026	Strategy 1 (dip-buying) paper trading begins
July 1, 2026	Strategy 1 goes live with real money ($222 starting balance)
July 16, 2026	Strategy 1 ended due to liquidity constraints
July 22, 2026	Strategy 2 (hold to settlement) paper trading begins
August 2026	Ongoing analysis and refinement; strategy wound down
---
Strategy 1 — Dip Buying with Target Sells
Concept
Temperature brackets priced at $0.02–$0.12 represent low-probability outcomes. These brackets regularly experience short-term price bounces even as they trend toward zero. The strategy: buy during a dip, place a limit sell order at 2–5x the entry price, collect the bounce.
Paper Trading Results
Win rate: 82.9% (490 target hits / 591 closed trades)
Unlimited fills assumed — every detected dip resulted in a full $5 position
Live Trading Results
Starting balance: $222.00
Peak account value: ~$400 (+$178, +80%)
Final account value: $85.59 (-$136.41, -61%)
Live win rate: 48.7%
Total trades executed: 674
Target hits (full fills): 284
Partial target hits: 148
Settled losses: 390
Root Cause Analysis
The gap between paper (82.9%) and live (48.7%) win rates was caused entirely by thin liquidity at low price points. Paper trading assumed complete fills at every detected price — in reality, live orders at $0.02–$0.12 filled 1–10 contracts instead of the expected 50–200. Individual target hits generated $0.10–$2.00 instead of the projected $8–12, making it mathematically impossible to overcome the $5 losses at settlement.
The strategy itself was sound — bounces occurred as predicted — but the market infrastructure didn't support the position sizes needed for profitability.
Key Learnings
Paper trading fill assumptions must account for real order book depth
Thin liquidity is a market structure problem, not a strategy problem
Pre-entry price trajectory (price 6h before entry vs entry price) was a meaningful predictor: winners had p6h avg 0.437 vs losers at 0.341
---
Strategy 2 — Hold to Settlement
Concept
The top 1–2 brackets by opening price represent the market's best estimate of the actual weather outcome. Rather than trading bounces on low-probability brackets, buy the market's most-likely-correct brackets when they temporarily dip to $0.20–$0.35, and hold to settlement at $1.00. Liquidity is irrelevant since Kalshi auto-settles all positions.
Design Decisions
Entry range: $0.20–$0.35 (higher price = more liquid, better fills)
No sell orders placed — hold everything to settlement
Rank 1 brackets only (highest opening price = market's top pick)
LOW temp markets only (HIGH temp showed 21–30% win rate vs 41% for LOW)
All 19 US cities monitored; worst performers identified for potential future exclusion
Paper Trading Results
Overall:
Win rate across paper runs: 39–51% (varies by period)
Best single day: 72.7%
Worst single day: 10.0%
Sample size: 800+ settled trades across multiple runs
By category (settled trades, Rank 1 LOW only):
Hour since open	Win Rate	Notes
0–4h	37%	First scan cycle; price discovery incomplete
4–8h	31%	Weakest window
8–12h	50%	Strongest consistent window
12–16h	36%	
16–20h	44%	
20–24h	40%	
By city (Rank 1 LOW, combined runs):
City	Win Rate	Sample
NYC	67%	12
DAL	67%	9
SATX	67%	9
CHI	42%	23
DEN	43%	23
MIA	22%	22
SEA	22%	21
LV	0%	5
Pre-entry price trajectory signal:
Early period (high win rate): winners had avg p6h = 0.429, losers = 0.443 — distinguishable
Late period (low win rate): winners had avg p6h = 0.463, losers = 0.479 — nearly identical
Conclusion: the signal degrades during volatile weather periods when even rank 1 brackets are uncertain
Why Strategy 2 Was Wound Down
Not due to a code or logic failure, but due to weather regime variance. Win rate showed a consistent "starts strong, then degrades" pattern across multiple runs, corresponding to shifts in weather predictability. During stable weather patterns, rank 1 brackets win reliably. During transitional or volatile weather (common in mid-August), even rank 1 brackets become uncertain and the strategy loses its edge.
The strategy remains viable and would be worth revisiting during stable weather seasons or with a weather-volatility filter.
---
Technical Highlights
Infrastructure Built
RSA-PSS signed API authentication (required by Kalshi V2)
Full order lifecycle: market buy → position tracking → settlement reconciliation
Partial fill detection and blended entry price calculation
Orderbook depth gating (queries live book before topping up)
Price history pipeline: 540,000+ rows across 1,018 brackets over 7 weeks
Pre-entry price snapshot system (2h, 4h, 6h before entry)
Timezone-aware scheduling (each city evaluated in its own local time)
Telegram bot with persistent pause state across container restarts
Docker Compose deployment with health monitoring and log rotation
Analysis Performed
Win rate analysis across rank, temp type, city, hour of market life, and entry price
Pre-entry price trajectory comparison between winning and losing trades
Liquidity analysis via bid-ask spread across the full 45-hour market lifecycle
NO-side trade viability study (rank 2 bracket peak timing, expected value calculation)
Settlement timing analysis by temp type and city timezone
---
Repo
`github.com/craigvandeman-lab/kalshi-market-bot`
Strategy 1 (dip-buying) is preserved at git tag `v1.0-dip-strategy` for future reference if Kalshi market liquidity improves.
