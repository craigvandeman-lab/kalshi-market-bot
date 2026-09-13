# Kalshi Weather Temperature Trading Bot

An autonomous algorithmic trading system for Kalshi's weather temperature prediction markets, built in Python and deployed on a Linux VPS. The bot executes real-money trades against Kalshi's V2 REST API, manages positions through their full lifecycle, and reports via Telegram.

---

## What It Does

Kalshi offers binary prediction markets on daily high and low temperatures across 19 US cities. Each market contains multiple "brackets" — e.g., "Will NYC's low temperature be between 65° and 66° tonight?" — that trade between $0.01 and $0.99 and settle at $1.00 (yes) or $0.00 (no).

This bot monitors those markets around the clock, detects price signals, executes trades via authenticated API calls, manages open positions, and settles them at expiration — all autonomously, with Telegram alerts for every significant event.

---

## Architecture

```
bot.py                  # Single-file core: all trading logic, scheduling, API calls
docker-compose.yml      # Container orchestration
.env                    # All configuration (no hardcoded values)
data/trades.db          # SQLite database: trades, watchlist, price history, bot state
```

**Stack:**
- Python 3.11 with `requests`, `schedule`, `python-telegram-bot`
- RSA-PSS signed API authentication (Kalshi V2 requirement)
- SQLite for persistence
- Docker Compose for deployment
- DigitalOcean VPS (Ubuntu 24)
- Telegram Bot API for real-time alerts and commands

---

## Key Features

### Trading Engine
- **Watchlist builder** — at 14:05 UTC daily, fetches all markets for the next event day across 19 city series, scores brackets by opening price, and selects the top N candidates per city
- **Dip detection** — monitors watched brackets every 2 minutes, enters positions when price dips into the configured entry range
- **Top-up system** — incrementally builds positions toward a target size as price continues to dip, tracking blended entry price across fills
- **Orderbook depth gating** — queries the live order book before topping up; skips if buyer depth at target is insufficient
- **Position management** — tracks all open positions with full cost basis, fees, and partial fill accounting

### Data Pipeline
- **Price history capture** — records `yes_ask`, `yes_bid`, and `last_price` every 2 minutes for all watched brackets, enabling post-hoc trajectory analysis
- **Pre-entry price snapshots** — at trade entry, records price 2h, 4h, and 6h prior for signal validation
- **Settlement reconciliation** — polls market status and closes positions correctly on Kalshi settlement

### Infrastructure
- **Telegram bot** — real-time trade alerts, `/dashboard`, `/balance`, `/pause`, `/resume` commands
- **Persistent pause state** — pause/resume survives container restarts via DB-backed bot state
- **Health monitoring** — alerts if monitor cycle hasn't completed in 20 minutes
- **Log rotation** — managed via logrotate on the VPS
- **Timezone-aware logic** — all market timing uses each city's local timezone, never a global UTC assumption

### Configuration
Every parameter is an environment variable — entry floor/cap, trade size, watchlist size, enabled series, wallet floor, scan interval, and more. No values are hardcoded.

---

## Strategy Evolution

The bot went through two distinct strategies during development:

### Strategy 1 — Dip Buying with Target Sells (v1.0)
**Hypothesis:** Low-probability brackets (priced $0.02–$0.12) experience temporary price bounces. Buy the dip, place a limit sell at 2–5x entry price, profit from the bounce.

**Results (live trading, $222 starting balance):**
- Peak account value: ~$400 (+$178, +80%)
- Final account value: ~$86 (-$136, -61%)
- Paper trading win rate: 82.9% (assumed full fills)
- Live trading win rate: 48.7%
- **Root cause of failure:** Thin liquidity at low price points. Paper trading assumed full fills; live orders filled 1–10 contracts instead of 50–100. Profitable bounces existed but position sizes were too small to overcome the losses.

Tagged in git as `v1.0-dip-strategy` for future reference if Kalshi liquidity improves.

### Strategy 2 — Hold to Settlement (v2.0, current)
**Hypothesis:** The top 1–2 brackets by opening price are the market's best estimate of the actual outcome. Buy them when they temporarily dip into the $0.20–$0.35 range and hold to settlement at $1.00.

**Results (paper trading):**
- Overall win rate: ~40–51% across paper runs
- Best category: Rank 1 LOW temp brackets — consistent 38–60% win rate across all hours since market open
- Hours 8–18 since market open showed 50–60% win rate
- HIGH temp brackets underperformed at 21–30% even with time filters; dropped from strategy
- Win rate showed week-over-week variance consistent with weather pattern shifts

**Key analytical findings:**
- Pre-entry price trajectory (p6h vs entry) is a meaningful signal — winners had price_6h_before_entry ~0.43 vs losers at ~0.38
- Rank 1 LOW performed consistently across the full 45-hour market lifetime; no optimal time cutoff found
- Per-city win rates showed high variance at current sample sizes; DEN, MIN, LV, SEA were consistently weakest
- The "starts strong then degrades" pattern observed across multiple runs likely reflects weather regime changes rather than strategy drift

---

## Database Schema (key tables)

**`trades`** — one row per position entry or top-up
```
id, series_ticker, market_ticker, bracket_label, side,
entry_price, sell_target, exit_price, exit_reason,
volume, entry_fee, exit_fee, realized_pnl,
price_2h_before_entry, price_4h_before_entry, price_6h_before_entry,
run_id, paper, created_at, closed_at
```

**`price_history`** — 2-minute snapshots of all watched brackets
```
market_ticker, series_ticker, temp_type,
yes_ask, yes_bid, last_price, observed_at, run_id
```

**`watchlist`** — daily bracket selections
```
event_ticker, series_ticker, event_date, occurrence_dt,
bracket_ticker, bracket_label, rank, yes_ask_at_open, open_time
```

**`bot_state`** — key-value store for persistent runtime state (e.g., trading_paused)

---

## Deployment

```bash
# Clone and configure
git clone https://github.com/craigvandeman-lab/kalshi-market-bot
cp .env.example .env
# Edit .env with your Kalshi API credentials and Telegram bot token

# Deploy
docker compose up -d --build

# Monitor
docker compose logs -f
```

**Telegram commands:**
- `/dashboard` — current run summary: open positions, win rate, PnL
- `/balance` — live wallet balance and exposure
- `/pause` — suspend new entries and top-ups (persists across restarts)
- `/resume` — resume trading

---

## What This Demonstrates

- **End-to-end system design** — from market data ingestion to trade execution to settlement reconciliation, all in a single maintainable codebase
- **API integration** — RSA-PSS signed authentication, rate limit handling, error recovery, and correct interpretation of a complex financial API
- **Data engineering** — SQLite schema design, efficient querying of 500K+ row price history tables, migration handling
- **Statistical analysis** — iterative strategy development driven by empirical win rate analysis across dimensions (rank, city, time, price trajectory)
- **Production operations** — containerized deployment, health monitoring, log rotation, persistent state management
- **Financial logic** — fee calculation, blended entry pricing, realized PnL tracking, partial fill accounting

---

## Notes

This project was developed by a non-programmer using AI-assisted coding (Claude + Cursor), with all architectural decisions, strategy design, data analysis, and operational judgment made by the human. The AI acted as a coding pair — translating design intent into working Python — while the trader directed every meaningful decision.
