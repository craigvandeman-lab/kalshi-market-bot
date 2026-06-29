"""
Kalshi Dip Bot — serial winner candlestick analysis.
Run: python serial_winner_analysis.py
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
API_SLEEP = 0.3

THRESHOLDS = [0.12, 0.15, 0.18, 0.20, 0.25, 0.30, 0.40, 0.50]
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


def yes_ask_close(candle: dict) -> float | None:
    yes_ask = candle.get("yes_ask") or {}
    raw = yes_ask.get("close_dollars")
    if raw is None or raw == "":
        yes_obj = candle.get("yes") or {}
        raw = yes_obj.get("close") or yes_obj.get("close_dollars")
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def fetch_candlesticks(series_ticker: str, market_ticker: str, start_ts: int, end_ts: int) -> list[dict]:
    endpoint = f"/series/{series_ticker}/markets/{market_ticker}/candlesticks"
    path = f"/trade-api/v2{endpoint}"
    url = f"{KALSHI_BASE_URL}{endpoint}"
    r = requests.get(
        url,
        headers=_get_headers("GET", path),
        params={"period_interval": 60, "start_ts": start_ts, "end_ts": end_ts},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("candlesticks", [])


def parse_candles(candlesticks: list[dict]) -> list[dict]:
    rows = []
    for candle in sorted(candlesticks, key=lambda c: int(c.get("end_period_ts", 0))):
        close = yes_ask_close(candle)
        if close is None:
            continue
        volume = float(candle.get("volume_fp", 0) or 0)
        rows.append({
            "time": int(candle.get("end_period_ts", 0)),
            "yes_ask_close": close,
            "volume": volume,
        })
    return rows


def count_dips(prices: list[float], high_thresh: float = 0.12, low_thresh: float = 0.09) -> int:
    seen_above = False
    dips = 0
    for price in prices:
        if price > high_thresh:
            seen_above = True
        if seen_above and price < low_thresh:
            dips += 1
    return dips


def threshold_crossings(candles: list[dict]) -> dict[float, int]:
    counts = {t: 0 for t in THRESHOLDS}
    for candle in candles:
        price = candle["yes_ask_close"]
        for t in THRESHOLDS:
            if price >= t:
                counts[t] += 1
    return counts


def suggest_target(crossings: dict[float, int]) -> tuple[float, float]:
    base = crossings.get(0.12, 0)
    if base == 0:
        return 0.12, 0.0
    half = base * 0.5
    suggested = 0.12
    for t in THRESHOLDS:
        if crossings.get(t, 0) >= half:
            suggested = t
    pct = crossings.get(suggested, 0) / base * 100
    return suggested, pct


def load_serial_winners(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute("""
        SELECT market_ticker,
               MAX(bracket_label) AS bracket_label,
               MAX(series_ticker) AS series_ticker,
               COUNT(*) AS wins,
               AVG(entry_price) AS avg_entry_price,
               AVG(sell_target) AS avg_sell_target
        FROM trades
        WHERE run_id = ?
          AND exit_reason = 'SELL_TARGET'
          AND exit_price IS NOT NULL
        GROUP BY market_ticker
        HAVING COUNT(*) >= 2
        ORDER BY wins DESC, market_ticker
    """, (RUN_ID,)).fetchall()


def analyze_serial_winner(row: sqlite3.Row) -> dict | None:
    market_ticker = row["market_ticker"]
    start_ts = market_open_unix(market_ticker)
    if start_ts is None:
        return None

    series_ticker = parse_series_from_ticker(market_ticker)
    end_ts = start_ts + 172800

    try:
        raw = fetch_candlesticks(series_ticker, market_ticker, start_ts, end_ts)
    except Exception as e:
        print(f"  API error {market_ticker}: {e}", file=sys.stderr)
        return None

    candles = parse_candles(raw)
    if not candles:
        return None

    prices = [c["yes_ask_close"] for c in candles]
    crossings = threshold_crossings(candles)
    suggested, suggest_pct = suggest_target(crossings)

    return {
        "market_ticker": market_ticker,
        "bracket_label": row["bracket_label"],
        "series_ticker": row["series_ticker"],
        "wins": row["wins"],
        "avg_entry": float(row["avg_entry_price"]),
        "avg_sell_target": float(row["avg_sell_target"]),
        "price_min": min(prices),
        "price_max": max(prices),
        "price_range": max(prices) - min(prices),
        "avg_price": sum(prices) / len(prices),
        "dip_count": count_dips(prices),
        "total_candles": len(candles),
        "crossings": crossings,
        "suggested_target": suggested,
        "suggest_pct": suggest_pct,
    }


def main() -> None:
    if not KALSHI_API_KEY or not KALSHI_PRIVATE_KEY_PEM:
        print("Kalshi API credentials missing from .env", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(DB_PATH):
        print(f"No database found at {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    serial_winners = load_serial_winners(conn)
    conn.close()

    if not serial_winners:
        print(f"No serial winners (2+ SELL_TARGET wins) found for run_id={RUN_ID}")
        return

    print(f"=== SERIAL WINNER ANALYSIS — Run #{RUN_ID} ===")
    print(f"Serial winners to analyze: {len(serial_winners)}")
    print()

    results: list[dict] = []
    for i, row in enumerate(serial_winners, 1):
        analysis = analyze_serial_winner(row)
        if analysis:
            results.append(analysis)
        time.sleep(API_SLEEP)
        if i % 5 == 0:
            print(f"  Fetched {i}/{len(serial_winners)} tickers...", file=sys.stderr)

    if not results:
        print("No candlestick data retrieved.")
        return

    results.sort(key=lambda r: (-r["wins"], r["market_ticker"]))
    print()

    # Section 1
    print("SERIAL WINNER PRICE RANGES")
    s1_rows = [
        [
            r["market_ticker"],
            r["bracket_label"],
            r["wins"],
            f"{r['avg_entry']:.2f}",
            f"{r['price_min']:.2f}",
            f"{r['price_max']:.2f}",
            f"{r['price_range']:.2f}",
            f"{r['avg_price']:.2f}",
            r["dip_count"],
        ]
        for r in results
    ]
    print(tabulate(
        s1_rows,
        headers=[
            "market_ticker", "bracket", "wins", "avg_entry",
            "min", "max", "range", "avg", "dips_below_0.09",
        ],
        tablefmt="plain",
    ))
    print()

    # Section 2
    print("THRESHOLD CROSSING FREQUENCY")
    s2_rows = []
    for r in results:
        row = [
            r["market_ticker"],
            r["bracket_label"],
            r["wins"],
        ]
        for t in THRESHOLDS:
            row.append(r["crossings"].get(t, 0))
        s2_rows.append(row)
    print(tabulate(
        s2_rows,
        headers=[
            "market_ticker", "bracket", "wins",
            ">=0.12", ">=0.15", ">=0.18", ">=0.20", ">=0.25", ">=0.30", ">=0.40", ">=0.50",
        ],
        tablefmt="plain",
    ))
    print()

    # Section 3
    print("SUMMARY RECOMMENDATIONS")
    for r in results:
        print(
            f"{r['market_ticker']}: suggested target = {r['suggested_target']:.2f} "
            f"(crosses >={r['suggested_target']:.2f} in {r['suggest_pct']:.0f}% of "
            f"candles that cross >=0.12)"
        )


if __name__ == "__main__":
    main()
