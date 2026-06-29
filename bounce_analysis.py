"""
Kalshi Dip Bot — bounce / headroom analysis for SELL_TARGET wins.
Run: python bounce_analysis.py
"""

import os
import re
import sys
import time
import base64
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv
from tabulate import tabulate

load_dotenv()

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "data", "trades.db"))
KALSHI_API_KEY = os.getenv("KALSHI_API_KEY") or os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PEM = os.getenv("KALSHI_PRIVATE_KEY_PEM", "")
KALSHI_BASE_URL = os.getenv("KALSHI_BASE_URL", "https://trading-api.kalshi.com/trade-api/v2")
RUN_ID = 1
UTC = ZoneInfo("UTC")
API_SLEEP = 0.25

ENTRY_BUCKETS = [
    ("0.01-0.02", 0.01, 0.02),
    ("0.03-0.04", 0.03, 0.04),
    ("0.05-0.06", 0.05, 0.06),
    ("0.07-0.09", 0.07, 0.09),
]

DATE_SEGMENT_RE = re.compile(r"^\d{2}[A-Z]{3}\d{2}$", re.IGNORECASE)


def _get_headers(method: str, path: str) -> dict:
    ts = str(int(time.time() * 1000))
    path_without_query = urlsplit(path).path or "/"
    msg = f"{ts}{method.upper()}{path_without_query}".encode("utf-8")
    pem = KALSHI_PRIVATE_KEY_PEM.replace("\\n", "\n").encode()
    private_key = serialization.load_pem_private_key(pem, password=None)
    signature = private_key.sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    sig_b64 = base64.b64encode(signature).decode()
    return {
        "KALSHI-ACCESS-KEY":       KALSHI_API_KEY,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig_b64,
        "Content-Type":            "application/json",
    }


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


def parse_event_date_from_ticker(market_ticker: str) -> datetime | None:
    for part in market_ticker.split("-"):
        if not DATE_SEGMENT_RE.match(part):
            continue
        try:
            return datetime.strptime(part.upper(), "%y%b%d")
        except ValueError:
            return None
    return None


def parse_series_from_ticker(market_ticker: str) -> str:
    if "-26" in market_ticker:
        return market_ticker.split("-26", 1)[0]
    return market_ticker.split("-", 1)[0]


def market_open_unix(market_ticker: str) -> int | None:
    event_date = parse_event_date_from_ticker(market_ticker)
    if event_date is None:
        return None
    market_open = (event_date - timedelta(days=1)).replace(
        hour=14, minute=0, second=0, microsecond=0, tzinfo=UTC
    )
    return int(market_open.timestamp())


def settlement_unix(market_ticker: str) -> int | None:
    event_date = parse_event_date_from_ticker(market_ticker)
    if event_date is None:
        return None
    settlement = (event_date + timedelta(days=1)).replace(
        hour=14, minute=0, second=0, microsecond=0, tzinfo=UTC
    )
    return int(settlement.timestamp())


def entry_bucket(entry: float) -> str | None:
    for label, lo, hi in ENTRY_BUCKETS:
        if lo <= entry <= hi + 1e-9:
            return label
    return None


def _float_field(obj: dict | None, *keys: str) -> float | None:
    if not obj:
        return None
    for key in keys:
        raw = obj.get(key)
        if raw is None or raw == "":
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def candle_high_price(candle: dict) -> float | None:
    yes_ask = candle.get("yes_ask") or {}
    high = _float_field(yes_ask, "high_dollars")
    if high is not None:
        return high
    price = candle.get("price") or {}
    return _float_field(price, "max_dollars", "high_dollars", "close_dollars", "mean_dollars")


def candle_close_price(candle: dict) -> float | None:
    price = candle.get("price") or {}
    close = _float_field(price, "close_dollars", "mean_dollars")
    if close is not None:
        return close
    yes_ask = candle.get("yes_ask") or {}
    return _float_field(yes_ask, "close_dollars")


def fetch_candlesticks(
    series_ticker: str,
    market_ticker: str,
    start_ts: int,
    end_ts: int,
) -> list[dict]:
    endpoint = f"/series/{series_ticker}/markets/{market_ticker}/candlesticks"
    path = f"/trade-api/v2{endpoint}"
    url = f"{KALSHI_BASE_URL}{endpoint}"
    r = requests.get(
        url,
        headers=_get_headers("GET", path),
        params={
            "period_interval": 60,
            "start_ts": start_ts,
            "end_ts": end_ts,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("candlesticks", [])


def analyze_trade(candlesticks: list[dict], entry_ts: int, our_exit: float) -> dict | None:
    if not candlesticks:
        return None

    candles = sorted(candlesticks, key=lambda c: int(c.get("end_period_ts", 0)))
    price_at_entry = None
    max_price = None

    for candle in candles:
        end_ts = int(candle.get("end_period_ts", 0))
        close = candle_close_price(candle)
        if price_at_entry is None and end_ts >= entry_ts and close is not None:
            price_at_entry = close

        if end_ts >= entry_ts:
            high = candle_high_price(candle)
            if high is not None:
                max_price = high if max_price is None else max(max_price, high)

    if max_price is None:
        return None

    headroom = max_price - our_exit
    headroom_pct = (headroom / our_exit * 100) if our_exit > 0 else 0.0
    return {
        "price_at_entry": price_at_entry,
        "max_price": max_price,
        "our_exit": our_exit,
        "headroom": headroom,
        "headroom_pct": headroom_pct,
    }


def load_sell_target_wins(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute("""
        SELECT market_ticker, bracket_label, entry_price, sell_target, created_at
        FROM trades
        WHERE run_id = ?
          AND exit_reason = 'SELL_TARGET'
          AND exit_price IS NOT NULL
        ORDER BY created_at
    """, (RUN_ID,)).fetchall()


def main() -> None:
    if not KALSHI_API_KEY or not KALSHI_PRIVATE_KEY_PEM:
        print("Kalshi API credentials missing from .env", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(DB_PATH):
        print(f"No database found at {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    trades = load_sell_target_wins(conn)
    conn.close()

    if not trades:
        print(f"No SELL_TARGET wins found for run_id={RUN_ID}")
        return

    print(f"=== BOUNCE ANALYSIS — Run #{RUN_ID} ===")
    print(f"SELL_TARGET wins to analyze: {len(trades)}")
    print()

    candle_cache: dict[str, list[dict]] = {}
    results: list[dict] = []
    skipped = 0

    for i, trade in enumerate(trades, 1):
        market_ticker = trade["market_ticker"]
        entry_dt = parse_db_time(trade["created_at"])
        if entry_dt is None:
            skipped += 1
            continue

        start_ts = market_open_unix(market_ticker)
        end_ts = settlement_unix(market_ticker)
        if start_ts is None or end_ts is None:
            skipped += 1
            continue

        if market_ticker not in candle_cache:
            series_ticker = parse_series_from_ticker(market_ticker)
            try:
                candle_cache[market_ticker] = fetch_candlesticks(
                    series_ticker, market_ticker, start_ts, end_ts
                )
            except Exception as e:
                print(f"  API error {market_ticker}: {e}", file=sys.stderr)
                candle_cache[market_ticker] = []
            time.sleep(API_SLEEP)

        metrics = analyze_trade(
            candle_cache[market_ticker],
            int(entry_dt.timestamp()),
            float(trade["sell_target"]),
        )
        if metrics is None:
            skipped += 1
            continue

        results.append({
            "market_ticker": market_ticker,
            "bracket_label": trade["bracket_label"],
            "entry": float(trade["entry_price"]),
            "bucket": entry_bucket(float(trade["entry_price"])),
            **metrics,
        })

        if i % 25 == 0:
            print(f"  Processed {i}/{len(trades)} trades...", file=sys.stderr)

    if skipped:
        print(f"Skipped {skipped} trades (missing data or API errors)", file=sys.stderr)
    print()

    if not results:
        print("No trades with candlestick data.")
        return

    # Section 1 — Headroom summary by entry bucket
    print("HEADROOM SUMMARY BY ENTRY BUCKET")
    bucket_rows = []
    for label, _, _ in ENTRY_BUCKETS:
        bucket = [r for r in results if r["bucket"] == label]
        if not bucket:
            continue
        n = len(bucket)
        bucket_rows.append([
            label,
            n,
            f"{sum(r['our_exit'] for r in bucket) / n:.3f}",
            f"{sum(r['max_price'] for r in bucket) / n:.3f}",
            f"{sum(r['headroom'] for r in bucket) / n:.3f}",
            f"{sum(r['headroom_pct'] for r in bucket) / n:.1f}%",
            sum(1 for r in bucket if r["max_price"] > r["our_exit"] * 1.25),
            sum(1 for r in bucket if r["max_price"] > r["our_exit"] * 1.50),
            sum(1 for r in bucket if r["max_price"] > r["our_exit"] * 2.00),
        ])
    print(tabulate(
        bucket_rows,
        headers=[
            "entry bucket", "trades", "avg our_exit", "avg max_price", "avg headroom",
            "avg headroom%", "max>exit*1.25", "max>exit*1.5", "max>exit*2.0",
        ],
        tablefmt="plain",
    ))
    print()

    # Section 2 — Top 20 biggest missed gains
    print("TOP 20 BIGGEST MISSED GAINS")
    top20 = sorted(results, key=lambda r: r["headroom"], reverse=True)[:20]
    top_rows = [
        [
            r["market_ticker"],
            r["bracket_label"],
            f"{r['entry']:.2f}",
            f"{r['our_exit']:.2f}",
            f"{r['max_price']:.2f}",
            f"{r['headroom']:.2f}",
            f"{r['headroom_pct']:.1f}%",
        ]
        for r in top20
    ]
    print(tabulate(
        top_rows,
        headers=["market_ticker", "bracket", "entry", "our_exit", "max_price", "headroom", "headroom%"],
        tablefmt="plain",
    ))
    print()

    # Overall summary
    n = len(results)
    pct_25 = sum(1 for r in results if r["max_price"] > r["our_exit"] * 1.25) / n * 100
    pct_50 = sum(1 for r in results if r["max_price"] > r["our_exit"] * 1.50) / n * 100
    pct_100 = sum(1 for r in results if r["max_price"] > r["our_exit"] * 2.00) / n * 100
    avg_headroom_pct = sum(r["headroom_pct"] for r in results) / n

    print("OVERALL SUMMARY")
    print(f"Of {n} SELL_TARGET wins analyzed:")
    print(f"  {pct_25:.0f}% went 25%+ above our exit target")
    print(f"  {pct_50:.0f}% went 50%+ above our exit target")
    print(f"  {pct_100:.0f}% went 100%+ above our exit target")
    print(f"  Avg headroom across all wins: {avg_headroom_pct:.1f}%")


if __name__ == "__main__":
    main()
