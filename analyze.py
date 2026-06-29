"""
Kalshi Dip Bot — closed-trade analysis for run #1.
Run: python analyze.py
"""

import os
import re
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from tabulate import tabulate

load_dotenv()

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "data", "trades.db"))
TRADE_AMOUNT = 5.00
RUN_ID = 1
UTC = ZoneInfo("UTC")

ENTRY_BUCKETS = [
    ("0.01-0.02", 0.01, 0.02),
    ("0.03-0.04", 0.03, 0.04),
    ("0.05-0.06", 0.05, 0.06),
    ("0.07-0.09", 0.07, 0.09),
]

AGE_BUCKETS = [
    ("0-3h", 0, 3),
    ("3-6h", 3, 6),
    ("6-12h", 6, 12),
    ("12-24h", 12, 24),
    ("24h+", 24, float("inf")),
]


def pnl(entry: float, exit_price: float) -> float:
    if entry <= 0:
        return 0.0
    shares = TRADE_AMOUNT / entry
    return shares * (exit_price - entry)


def fmt_pnl(amount: float, signed: bool = True) -> str:
    if signed:
        sign = "+" if amount >= 0 else ""
        return f"{sign}${amount:.2f}"
    return f"${amount:.2f}"


def fmt_pct(rate: float) -> str:
    return f"{rate * 100:.0f}%"


def parse_db_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


DATE_SEGMENT_RE = re.compile(r"^\d{2}[A-Z]{3}\d{2}$", re.IGNORECASE)


def parse_event_date_from_ticker(market_ticker: str) -> datetime | None:
    for part in market_ticker.split("-"):
        if not DATE_SEGMENT_RE.match(part):
            continue
        try:
            return datetime.strptime(part.upper(), "%y%b%d")
        except ValueError:
            return None
    return None


def market_open_from_ticker(market_ticker: str) -> datetime | None:
    event_date = parse_event_date_from_ticker(market_ticker)
    if event_date is None:
        return None
    market_open_day = event_date - timedelta(days=1)
    return market_open_day.replace(hour=14, minute=0, second=0, microsecond=0, tzinfo=UTC)


def age_at_entry_hours(row: sqlite3.Row) -> float | None:
    entry_dt = parse_db_time(row["created_at"])
    market_open = market_open_from_ticker(row["market_ticker"])
    if entry_dt is None or market_open is None:
        return None
    return max(0.0, (entry_dt - market_open).total_seconds() / 3600)


def entry_bucket(entry: float) -> str | None:
    for label, lo, hi in ENTRY_BUCKETS:
        if lo <= entry <= hi + 1e-9:
            return label
    return None


def age_bucket(hours: float | None) -> str | None:
    if hours is None:
        return None
    for label, lo, hi in AGE_BUCKETS:
        if lo <= hours < hi:
            return label
    return None


def load_closed_trades(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute("""
        SELECT id, series_ticker, market_ticker, bracket_label, entry_price, sell_target,
               exit_price, exit_reason, volume, yes_bid, open_interest, vwap,
               is_mean_bracket, created_at, closed_at
        FROM trades
        WHERE run_id = ? AND exit_price IS NOT NULL
        ORDER BY created_at
    """, (RUN_ID,)).fetchall()


def section_overall(trades: list[sqlite3.Row]) -> None:
    pnls = [pnl(r["entry_price"], r["exit_price"]) for r in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = len(trades)
    win_n = len(wins)
    loss_n = len(losses)
    total_pnl = sum(pnls)
    avg_win = sum(wins) / win_n if win_n else 0.0
    avg_loss = sum(losses) / loss_n if loss_n else 0.0
    expectancy = total_pnl / total if total else 0.0

    print("OVERALL SUMMARY")
    print(
        f"Total closed: {total}  |  Wins: {win_n} ({fmt_pct(win_n / total)})  |  "
        f"Losses: {loss_n} ({fmt_pct(loss_n / total)})"
    )
    print(
        f"Total PnL: {fmt_pnl(total_pnl, signed=False)}  |  "
        f"Avg win: {fmt_pnl(avg_win)}  |  Avg loss: {fmt_pnl(avg_loss)}"
    )
    print(f"Expectancy per trade: {fmt_pnl(expectancy)}")
    print()


def section_entry_buckets(trades: list[sqlite3.Row]) -> None:
    print("PERFORMANCE BY ENTRY PRICE BUCKET")
    rows = []
    for label, _, _ in ENTRY_BUCKETS:
        bucket = [r for r in trades if entry_bucket(r["entry_price"]) == label]
        if not bucket:
            continue
        pnls = [pnl(r["entry_price"], r["exit_price"]) for r in bucket]
        wins = sum(1 for p in pnls if p > 0)
        rows.append([
            label,
            len(bucket),
            fmt_pct(wins / len(bucket)),
            fmt_pnl(sum(pnls) / len(bucket), signed=False),
            fmt_pnl(sum(pnls), signed=False),
            f"{sum(r['entry_price'] for r in bucket) / len(bucket):.3f}",
        ])
    print(tabulate(
        rows,
        headers=["bucket", "count", "win%", "avg PnL", "total PnL", "avg entry"],
        tablefmt="plain",
    ))
    print()


def section_mean_bracket(trades: list[sqlite3.Row]) -> None:
    print("MEAN BRACKET VS #2 BRACKET")
    rows = []
    for label, flag in [("mean (1)", 1), ("#2 (0)", 0)]:
        subset = [r for r in trades if (r["is_mean_bracket"] or 0) == flag]
        if not subset:
            rows.append([label, 0, "—", "—", "$0.00"])
            continue
        pnls = [pnl(r["entry_price"], r["exit_price"]) for r in subset]
        wins = sum(1 for p in pnls if p > 0)
        rows.append([
            label,
            len(subset),
            fmt_pct(wins / len(subset)),
            fmt_pnl(sum(pnls) / len(subset), signed=False),
            fmt_pnl(sum(pnls), signed=False),
        ])
    print(tabulate(
        rows,
        headers=["bracket", "count", "win%", "avg PnL", "total PnL"],
        tablefmt="plain",
    ))
    print()


def section_market_age(trades: list[sqlite3.Row]) -> None:
    print("PERFORMANCE BY MARKET AGE AT ENTRY")
    rows = []
    for label, _, _ in AGE_BUCKETS:
        bucket = [
            r for r in trades
            if age_bucket(age_at_entry_hours(r)) == label
        ]
        if not bucket:
            continue
        pnls = [pnl(r["entry_price"], r["exit_price"]) for r in bucket]
        wins = sum(1 for p in pnls if p > 0)
        rows.append([
            label,
            len(bucket),
            fmt_pct(wins / len(bucket)),
            fmt_pnl(sum(pnls) / len(bucket), signed=False),
            fmt_pnl(sum(pnls), signed=False),
        ])
    print(tabulate(
        rows,
        headers=["age", "count", "win%", "avg PnL", "total PnL"],
        tablefmt="plain",
    ))
    print()


def section_target_analysis(trades: list[sqlite3.Row]) -> None:
    print("TARGET ANALYSIS")
    wins = [r for r in trades if pnl(r["entry_price"], r["exit_price"]) > 0]
    if not wins:
        print("(no wins)")
        print()
        return

    dist_rows = []
    for label, _, _ in ENTRY_BUCKETS:
        bucket = [r for r in wins if entry_bucket(r["entry_price"]) == label]
        if not bucket:
            continue
        avg_entry = sum(r["entry_price"] for r in bucket) / len(bucket)
        avg_exit = sum(r["exit_price"] for r in bucket) / len(bucket)
        avg_target = sum(r["sell_target"] for r in bucket) / len(bucket)
        dist_rows.append([
            label,
            len(bucket),
            f"{avg_entry:.3f}",
            f"{avg_target:.3f}",
            f"{avg_exit:.3f}",
        ])
    print("Win distribution by entry bucket:")
    print(tabulate(
        dist_rows,
        headers=["entry bucket", "wins", "avg entry", "avg sell_target", "avg exit"],
        tablefmt="plain",
    ))
    print()

    actual_total = sum(pnl(r["entry_price"], r["exit_price"]) for r in wins)
    loss_total = sum(
        pnl(r["entry_price"], r["exit_price"])
        for r in trades if pnl(r["entry_price"], r["exit_price"]) <= 0
    )

    hypo_rows = []
    for pct_label, mult in [("actual", 1.0), ("+25% target", 1.25), ("+50% target", 1.50)]:
        if mult == 1.0:
            win_pnl = actual_total
        else:
            win_pnl = sum(
                pnl(r["entry_price"], min(r["exit_price"] * mult, 1.0))
                for r in wins
            )
        hypo_rows.append([
            pct_label,
            fmt_pnl(win_pnl, signed=False),
            fmt_pnl(win_pnl + loss_total, signed=False),
        ])
    print("Hypothetical total PnL (wins exit at scaled price, capped at $1.00; same win set):")
    print(tabulate(
        hypo_rows,
        headers=["scenario", "win PnL", "total PnL (incl. losses)"],
        tablefmt="plain",
    ))
    print()


def section_loss_analysis(trades: list[sqlite3.Row]) -> None:
    print("LOSS ANALYSIS — COMMON THREADS")
    losses = [r for r in trades if pnl(r["entry_price"], r["exit_price"]) <= 0]
    if not losses:
        print("(no losses)")
        print()
        return

    ages = [age_at_entry_hours(r) for r in losses]
    ages_ok = [a for a in ages if a is not None]
    vwaps = [r["vwap"] for r in losses if r["vwap"] is not None]

    def avg(vals: list[float]) -> str:
        return f"{sum(vals) / len(vals):.2f}" if vals else "—"

    print(f"Average volume at entry:  {avg([r['volume'] or 0 for r in losses])}")
    print(f"Average OI at entry:      {avg([r['open_interest'] or 0 for r in losses])}")
    print(f"Average yes_bid at entry: {avg([r['yes_bid'] or 0 for r in losses])}")
    print(f"Average VWAP at entry:    {avg(vwaps)}  (n={len(vwaps)})")
    print(f"Average market age (h):   {avg(ages_ok)}")
    print()

    by_series: dict[str, int] = {}
    for r in losses:
        by_series[r["series_ticker"]] = by_series.get(r["series_ticker"], 0) + 1
    top_series = sorted(by_series.items(), key=lambda x: (-x[1], x[0]))[:10]
    print("Top 10 series by loss count:")
    print(tabulate(
        [[s, n] for s, n in top_series],
        headers=["series", "losses"],
        tablefmt="plain",
    ))
    print()

    vol_zero = sum(1 for r in losses if (r["volume"] or 0) == 0)
    oi_low = sum(1 for r in losses if (r["open_interest"] or 0) < 5)
    bid_zero = sum(1 for r in losses if (r["yes_bid"] or 0) == 0)
    print("Loss breakdown:")
    print(tabulate(
        [
            ["volume = 0", vol_zero, fmt_pct(vol_zero / len(losses))],
            ["OI < 5", oi_low, fmt_pct(oi_low / len(losses))],
            ["yes_bid = 0", bid_zero, fmt_pct(bid_zero / len(losses))],
        ],
        headers=["condition", "count", "% of losses"],
        tablefmt="plain",
    ))
    print()


def section_serial_winners(trades: list[sqlite3.Row]) -> None:
    print("SERIAL WINNERS (MULTI-HIT BRACKETS)")
    wins = [r for r in trades if pnl(r["entry_price"], r["exit_price"]) > 0]
    by_ticker: dict[str, list[sqlite3.Row]] = {}
    for r in wins:
        by_ticker.setdefault(r["market_ticker"], []).append(r)

    multi = {t: rows for t, rows in by_ticker.items() if len(rows) > 1}
    if not multi:
        print("(none)")
        print()
        return

    rows = []
    for ticker, group in sorted(multi.items(), key=lambda x: (-len(x[1]), x[0])):
        pnls = [pnl(r["entry_price"], r["exit_price"]) for r in group]
        rows.append([
            ticker,
            group[0]["bracket_label"],
            len(group),
            fmt_pnl(sum(pnls), signed=False),
            f"{sum(r['entry_price'] for r in group) / len(group):.3f}",
            f"{sum(r['exit_price'] for r in group) / len(group):.3f}",
        ])
    print(tabulate(
        rows,
        headers=["ticker", "bracket", "wins", "total PnL", "avg entry", "avg exit"],
        tablefmt="plain",
    ))
    print()


def section_by_series(trades: list[sqlite3.Row]) -> None:
    print("PERFORMANCE BY SERIES")
    by_series: dict[str, list[sqlite3.Row]] = {}
    for r in trades:
        by_series.setdefault(r["series_ticker"], []).append(r)

    rows = []
    for series, group in by_series.items():
        pnls = [pnl(r["entry_price"], r["exit_price"]) for r in group]
        wins = sum(1 for p in pnls if p > 0)
        rows.append([
            series,
            len(group),
            wins,
            fmt_pct(wins / len(group)),
            fmt_pnl(sum(pnls), signed=False),
        ])

    rows.sort(key=lambda x: float(x[4].replace("$", "").replace("+", "")), reverse=True)
    top15 = rows[:15]
    bottom10 = rows[-10:] if len(rows) > 10 else rows

    print("Top 15 by total PnL:")
    print(tabulate(
        top15,
        headers=["series", "trades", "wins", "win%", "total PnL"],
        tablefmt="plain",
    ))
    print()
    print("Bottom 10 by total PnL:")
    print(tabulate(
        bottom10,
        headers=["series", "trades", "wins", "win%", "total PnL"],
        tablefmt="plain",
    ))
    print()


def main() -> None:
    if not os.path.exists(DB_PATH):
        print(f"No database found at {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    trades = load_closed_trades(conn)
    conn.close()

    if not trades:
        print(f"No closed trades found for run_id={RUN_ID}")
        return

    print(f"=== KALSHI DIP BOT ANALYSIS — Run #{RUN_ID} ===")
    print(f"Closed trades: {len(trades)}")
    print()

    section_overall(trades)
    section_entry_buckets(trades)
    section_mean_bracket(trades)
    section_market_age(trades)
    section_target_analysis(trades)
    section_loss_analysis(trades)
    section_serial_winners(trades)
    section_by_series(trades)


if __name__ == "__main__":
    main()
