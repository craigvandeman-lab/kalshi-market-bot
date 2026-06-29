"""
Kalshi Dip Bot — terminal dashboard.
Run: python dashboard.py
"""

import os
import sys
import sqlite3
import time
import base64
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv
from tabulate import tabulate

load_dotenv()

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "data", "trades.db"))
TRADE_AMOUNT = 5.00
KALSHI_API_KEY = os.getenv("KALSHI_API_KEY") or os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PEM = os.getenv("KALSHI_PRIVATE_KEY_PEM", "")
KALSHI_BASE_URL = os.getenv("KALSHI_BASE_URL", "https://trading-api.kalshi.com/trade-api/v2")

EASTERN = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def _parse_db_time(value: str | None) -> datetime | None:
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


def _format_age(created_at: str | None) -> str:
    dt = _parse_db_time(created_at)
    if dt is None:
        return "—"
    delta = datetime.now(tz=UTC) - dt
    if delta < timedelta(minutes=1):
        return f"{int(delta.total_seconds())}s"
    hours, rem = divmod(int(delta.total_seconds()), 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _pnl(entry: float, exit_price: float) -> float:
    if entry <= 0:
        return 0.0
    shares = TRADE_AMOUNT / entry
    return shares * (exit_price - entry)


def _fmt_pnl(amount: float, signed: bool = True) -> str:
    if signed:
        sign = "+" if amount >= 0 else ""
        return f"{sign}${amount:.2f}"
    return f"${amount:.2f}"


def _avg_pnl(rows: list[tuple]) -> float:
    if not rows:
        return 0.0
    return sum(_pnl(e, x) for e, x in rows) / len(rows)


def parse_event_date(market_ticker: str) -> str:
    parts = market_ticker.split("-")
    if len(parts) < 2:
        return "—"
    try:
        dt = datetime.strptime(parts[1].upper(), "%y%b%d")
        return f"{dt.strftime('%b')} {dt.day}"
    except ValueError:
        return "—"


def _kalshi_headers(method: str, path: str) -> dict:
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


def fetch_market(market_ticker: str) -> dict:
    if not KALSHI_API_KEY or not KALSHI_PRIVATE_KEY_PEM:
        return {}
    endpoint = f"/markets/{market_ticker}"
    path = f"/trade-api/v2{endpoint}"
    url = f"{KALSHI_BASE_URL}{endpoint}"
    try:
        r = requests.get(url, headers=_kalshi_headers("GET", path), timeout=10)
        r.raise_for_status()
        return r.json().get("market", {})
    except Exception:
        return {}


def fetch_yes_bid(market_ticker: str) -> str:
    market = fetch_market(market_ticker)
    if not market:
        return "—"
    yes_bid = float(market.get("yes_bid_dollars", 0) or 0)
    return f"{yes_bid:.2f}"


def _fmt_volume(volume) -> str:
    if volume is None:
        return "—"
    return f"{float(volume):,.0f}"


def _fmt_vwap(vwap) -> str:
    if vwap is None:
        return "—"
    return f"{float(vwap):.2f}"


def market_age_hours(occurrence_dt: datetime) -> float:
    market_open = occurrence_dt.replace(hour=14, minute=0, second=0, microsecond=0) - timedelta(days=1)
    age = (datetime.now(tz=UTC) - market_open).total_seconds() / 3600
    return max(0.0, age)


def _parse_occurrence_datetime(market: dict) -> datetime | None:
    raw = market.get("occurrence_datetime")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _fmt_age_h(occurrence_dt: datetime | None) -> str:
    if occurrence_dt is None:
        return "—"
    return f"{market_age_hours(occurrence_dt):.1f}"


def _fmt_is_mean(value) -> str:
    if value is None:
        return "0"
    return "1" if int(value) else "0"


def _fmt_mean(value) -> str:
    if value is not None and int(value):
        return "✓"
    return ""


def _parse_run_arg() -> int | None:
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--run" and i + 1 < len(args):
            return int(args[i + 1])
    return None


def _resolve_run_id(conn: sqlite3.Connection, run_arg: int | None) -> int | None:
    if run_arg is not None:
        row = conn.execute("SELECT id FROM paper_runs WHERE id = ?", (run_arg,)).fetchone()
        return row[0] if row else None
    row = conn.execute("SELECT MAX(id) FROM paper_runs").fetchone()
    return row[0] if row and row[0] is not None else None


def main():
    if not os.path.exists(DB_PATH):
        print(f"No database found at {DB_PATH}")
        print("Run the bot first to create trades.")
        return

    run_arg = _parse_run_arg()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    run_id = _resolve_run_id(conn, run_arg)
    if run_id is None:
        conn.close()
        if run_arg is not None:
            print(f"Paper run #{run_arg} not found.")
        else:
            print("No paper runs found. Start the bot first.")
        return

    open_rows = conn.execute("""
        SELECT series_ticker, market_ticker, bracket_label, entry_price, sell_target, volume, vwap,
               is_mean_bracket, created_at
        FROM trades
        WHERE exit_price IS NULL AND run_id = ?
        ORDER BY created_at DESC
    """, (run_id,)).fetchall()

    sell_target_wins = conn.execute("""
        SELECT entry_price, exit_price
        FROM trades
        WHERE exit_reason = 'SELL_TARGET' AND exit_price IS NOT NULL AND run_id = ?
    """, (run_id,)).fetchall()

    settled_wins = conn.execute("""
        SELECT entry_price, exit_price
        FROM trades
        WHERE exit_reason = 'SETTLED_WIN' AND exit_price IS NOT NULL AND run_id = ?
    """, (run_id,)).fetchall()

    settled_losses = conn.execute("""
        SELECT entry_price, exit_price
        FROM trades
        WHERE exit_reason = 'SETTLED_LOSS' AND exit_price IS NOT NULL AND run_id = ?
    """, (run_id,)).fetchall()

    all_closed = conn.execute("""
        SELECT entry_price, exit_price
        FROM trades
        WHERE exit_price IS NOT NULL AND run_id = ?
    """, (run_id,)).fetchall()

    recent_closed = conn.execute("""
        SELECT series_ticker, market_ticker, bracket_label, entry_price, exit_price,
               exit_reason, volume, vwap, is_mean_bracket, run_id, created_at, closed_at
        FROM trades
        WHERE exit_price IS NOT NULL AND run_id = ?
        ORDER BY closed_at DESC
        LIMIT 20
    """, (run_id,)).fetchall()

    conn.close()

    now_et = datetime.now(tz=EASTERN).strftime("%Y-%m-%d %H:%M ET")
    total_pnl = sum(_pnl(r["entry_price"], r["exit_price"]) for r in all_closed)

    print(f"=== KALSHI DIP BOT DASHBOARD — Run #{run_id} ===")
    print(f"As of: {now_et}")
    print()
    print(f"OPEN POSITIONS:    {len(open_rows):>3}")
    print(
        f"WINS (target hit): {len(sell_target_wins):>3}   "
        f"avg PnL: {_fmt_pnl(_avg_pnl([(r['entry_price'], r['exit_price']) for r in sell_target_wins]))}"
    )
    print(
        f"WINS (settled):    {len(settled_wins):>3}   "
        f"avg PnL: {_fmt_pnl(_avg_pnl([(r['entry_price'], r['exit_price']) for r in settled_wins]))}"
    )
    print(
        f"LOSSES (settled):  {len(settled_losses):>3}   "
        f"avg PnL: {_fmt_pnl(_avg_pnl([(r['entry_price'], r['exit_price']) for r in settled_losses]))}"
    )
    print(f"TOTAL PnL:             {_fmt_pnl(total_pnl)}")
    print()

    print("OPEN POSITIONS")
    if open_rows:
        open_table = []
        for r in open_rows:
            market = fetch_market(r["market_ticker"])
            occurrence_dt = _parse_occurrence_datetime(market)
            yes_bid = float(market.get("yes_bid_dollars", 0) or 0) if market else None
            open_table.append([
                r["series_ticker"],
                r["bracket_label"],
                parse_event_date(r["market_ticker"]),
                f"{r['entry_price']:.2f}",
                f"{yes_bid:.2f}" if yes_bid is not None else "—",
                f"{r['sell_target']:.2f}",
                _fmt_volume(r["volume"]),
                _fmt_vwap(r["vwap"]),
                _fmt_age_h(occurrence_dt),
                _fmt_mean(r["is_mean_bracket"]),
                _format_age(r["created_at"]),
            ])
            time.sleep(0.1)
        print(tabulate(
            open_table,
            headers=["series", "bracket", "date", "entry", "bid", "target", "volume", "vwap", "age_h", "mean", "age"],
            tablefmt="plain",
        ))
    else:
        print("(none)")
    print()

    print("RECENT CLOSED TRADES")
    if recent_closed:
        closed_table = []
        for r in recent_closed:
            pnl = _pnl(r["entry_price"], r["exit_price"])
            closed_table.append([
                r["series_ticker"],
                r["bracket_label"],
                parse_event_date(r["market_ticker"]),
                f"{r['entry_price']:.2f}",
                f"{r['exit_price']:.2f}",
                _fmt_volume(r["volume"]),
                _fmt_vwap(r["vwap"]),
                _fmt_is_mean(r["is_mean_bracket"]),
                r["run_id"] if r["run_id"] is not None else "—",
                r["exit_reason"] or "—",
                _fmt_pnl(pnl),
            ])
        print(tabulate(
            closed_table,
            headers=["series", "bracket", "date", "entry", "exit", "volume", "vwap", "is_mean", "run_id", "result", "PnL"],
            tablefmt="plain",
        ))
    else:
        print("(none)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
