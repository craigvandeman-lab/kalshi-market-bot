"""
Kalshi Market Bot
-----------------
Market-based bracket selection (ensemble logic disabled).
"""

import os
import sys
import time
import uuid
import json
import base64
import logging
import sqlite3
import statistics
import requests
import numpy as np
import schedule
import openmeteo_requests
import requests_cache
from retry_requests import retry
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit
from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

load_dotenv()

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "data", "trades.db"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log_path = os.path.join(os.path.dirname(DB_PATH), "bot.log")
os.makedirs(os.path.dirname(log_path), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_path),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config from environment variables
# ---------------------------------------------------------------------------
KALSHI_API_KEY         = os.getenv("KALSHI_API_KEY") or os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PEM = os.getenv("KALSHI_PRIVATE_KEY_PEM", "")
KALSHI_BASE_URL        = os.getenv("KALSHI_BASE_URL", "https://trading-api.kalshi.com/trade-api/v2")
PAPER_TRADING          = os.getenv("PAPER_TRADING", "true").lower() == "true"
PAPER_STARTING_BALANCE = float(os.getenv("PAPER_STARTING_BALANCE", "500.00"))
TRADE_AMOUNT_CENTS     = int(os.getenv("TRADE_AMOUNT_CENTS", "500"))
TELEGRAM_BOT_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID       = os.getenv("TELEGRAM_CHAT_ID", "")

ENTRY_CAP        = float(os.getenv("ENTRY_CAP", "0.35"))
ENTRY_FLOOR      = float(os.getenv("ENTRY_FLOOR", "0.20"))
WATCHLIST_SIZE   = int(os.getenv("WATCHLIST_SIZE", "3"))
VWAP_MIN_RATIO   = float(os.getenv("VWAP_MIN_RATIO", "0.85"))
OI_MIN           = float(os.getenv("OI_MIN", "5"))
VWAP_MIN_CANDLES = int(os.getenv("VWAP_MIN_CANDLES", "2"))
TARGET_TIER_1    = float(os.getenv("TARGET_TIER_1", "0.20"))  # entry <= 0.02
TARGET_TIER_2    = float(os.getenv("TARGET_TIER_2", "0.18"))  # entry <= 0.04
TARGET_TIER_3    = float(os.getenv("TARGET_TIER_3", "0.20"))  # entry <= 0.06
TARGET_TIER_4    = float(os.getenv("TARGET_TIER_4", "0.25"))  # entry <= 0.09
TARGET_TIER_5    = float(os.getenv("TARGET_TIER_5", "0.22"))  # entry <= 0.11
TARGET_TIER_6    = float(os.getenv("TARGET_TIER_6", "0.24"))  # entry <= 0.12
MAX_SPREAD       = 0.06
MIN_WALLET_BALANCE = float(os.getenv("MIN_WALLET_BALANCE", "50.00"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "999"))
ENABLED_SERIES     = os.getenv("ENABLED_SERIES", "")
SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL_MINUTES", "2"))
TRADING_PAUSED = os.getenv("TRADING_PAUSED", "false").lower() == "true"

EASTERN = ZoneInfo("America/New_York")
UTC     = ZoneInfo("UTC")

ENSEMBLE_API_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
_cache_session = requests_cache.CachedSession(".cache/openmeteo_cache", expire_after=3600)
_ensemble_client = openmeteo_requests.Client(session=retry(_cache_session, retries=3, backoff_factor=0.3))

watchlist: dict = {}
# Structure:
# {
#   "KXHIGHTSFO-26JUN25": {
#       "series_ticker": "KXHIGHTSFO",
#       "event_date": "2026-06-25",
#       "occurrence_dt": datetime,
#       "brackets": [
#           {"ticker": "KXHIGHTSFO-26JUN25-B74.5", "bracket_label": "74° to 75°", "rank": 1},
#           {"ticker": "KXHIGHTSFO-26JUN25-T72", "bracket_label": "73° or above", "rank": 2},
#           {"ticker": "KXHIGHTSFO-26JUN25-B72.5", "bracket_label": "72° to 73°", "rank": 3},
#       ]
#   },
#   ...
# }
open_positions: dict = {}
telegram_offset: int = 0
current_run_id: int = 0
watchlist_is_valid: bool = False
cycle_spent: float = 0.0
paper_balance: float = PAPER_STARTING_BALANCE
last_cycle_at: datetime | None = None
trading_paused: bool = False

# vwap_cache: dict = {}
# VWAP_CACHE_TTL_MINUTES = 15

# ---------------------------------------------------------------------------
# City / series config  (airport coordinates)
# ---------------------------------------------------------------------------
SERIES_CONFIG = {
    # Exact series tickers as used by Kalshi (confirmed from live trade data)
    # Coordinates are airport weather stations (not city centers)
    # HIGH series — forecast temp to use: high_f
    "KXHIGHAUS":   {"timezone": "America/Chicago",     "city": "Austin",        "lat": 30.1975,  "lon": -97.6664,  "temp_type": "high"},
    "KXHIGHCHI":   {"timezone": "America/Chicago",     "city": "Chicago",       "lat": 41.9800,  "lon": -87.9090,  "temp_type": "high"},
    "KXHIGHDEN":   {"timezone": "America/Denver",      "city": "Denver",        "lat": 39.8561,  "lon": -104.6737, "temp_type": "high"},
    "KXHIGHLAX":   {"timezone": "America/Los_Angeles", "city": "Los Angeles",   "lat": 33.9425,  "lon": -118.4081, "temp_type": "high"},
    "KXHIGHMIA":   {"timezone": "America/New_York",    "city": "Miami",         "lat": 25.7959,  "lon": -80.2870,  "temp_type": "high"},
    "KXHIGHNY":    {"timezone": "America/New_York",    "city": "New York",      "lat": 40.6413,  "lon": -73.7781,  "temp_type": "high"},
    "KXHIGHPHIL":  {"timezone": "America/New_York",    "city": "Philadelphia",  "lat": 39.8721,  "lon": -75.2411,  "temp_type": "high"},
    "KXHIGHTATL":  {"timezone": "America/New_York",    "city": "Atlanta",       "lat": 33.6367,  "lon": -84.4281,  "temp_type": "high"},
    "KXHIGHTBOS":  {"timezone": "America/New_York",    "city": "Boston",        "lat": 42.3656,  "lon": -71.0096,  "temp_type": "high"},
    "KXHIGHTDAL":  {"timezone": "America/Chicago",     "city": "Dallas",        "lat": 32.8998,  "lon": -97.0403,  "temp_type": "high"},
    "KXHIGHTDC":   {"timezone": "America/New_York",    "city": "Washington DC", "lat": 38.9531,  "lon": -77.4565,  "temp_type": "high"},
    "KXHIGHTHOU":  {"timezone": "America/Chicago",     "city": "Houston",       "lat": 29.9844,  "lon": -95.3414,  "temp_type": "high"},
    "KXHIGHTLV":   {"timezone": "America/Los_Angeles", "city": "Las Vegas",     "lat": 36.0840,  "lon": -115.1537, "temp_type": "high"},
    "KXHIGHTMIN":  {"timezone": "America/Chicago",     "city": "Minneapolis",   "lat": 44.8848,  "lon": -93.2223,  "temp_type": "high"},
    "KXHIGHTNOLA": {"timezone": "America/Chicago",     "city": "New Orleans",   "lat": 29.9934,  "lon": -90.2580,  "temp_type": "high"},
    "KXHIGHTOKC":  {"timezone": "America/Chicago",     "city": "Oklahoma City", "lat": 35.3931,  "lon": -97.6007,  "temp_type": "high"},
    "KXHIGHTPHX":  {"timezone": "America/Phoenix",     "city": "Phoenix",       "lat": 33.4373,  "lon": -112.0078, "temp_type": "high"},
    "KXHIGHTSATX": {"timezone": "America/Chicago",     "city": "San Antonio",   "lat": 29.5337,  "lon": -98.4698,  "temp_type": "high"},
    "KXHIGHTSEA":  {"timezone": "America/Los_Angeles", "city": "Seattle",       "lat": 47.4502,  "lon": -122.3088, "temp_type": "high"},
    "KXHIGHTSFO":  {"timezone": "America/Los_Angeles", "city": "San Francisco", "lat": 37.6213,  "lon": -122.3790, "temp_type": "high"},
    # LOW series — forecast temp to use: low_f
    "KXLOWTATL":   {"timezone": "America/New_York",    "city": "Atlanta",       "lat": 33.6367,  "lon": -84.4281,  "temp_type": "low"},
    "KXLOWTAUS":   {"timezone": "America/Chicago",     "city": "Austin",        "lat": 30.1975,  "lon": -97.6664,  "temp_type": "low"},
    "KXLOWTBOS":   {"timezone": "America/New_York",    "city": "Boston",        "lat": 42.3656,  "lon": -71.0096,  "temp_type": "low"},
    "KXLOWTCHI":   {"timezone": "America/Chicago",     "city": "Chicago",       "lat": 41.9800,  "lon": -87.9090,  "temp_type": "low"},
    "KXLOWTDAL":   {"timezone": "America/Chicago",     "city": "Dallas",        "lat": 32.8998,  "lon": -97.0403,  "temp_type": "low"},
    "KXLOWTDC":    {"timezone": "America/New_York",    "city": "Washington DC", "lat": 38.9531,  "lon": -77.4565,  "temp_type": "low"},
    "KXLOWTDEN":   {"timezone": "America/Denver",      "city": "Denver",        "lat": 39.8561,  "lon": -104.6737, "temp_type": "low"},
    "KXLOWTHOU":   {"timezone": "America/Chicago",     "city": "Houston",       "lat": 29.9844,  "lon": -95.3414,  "temp_type": "low"},
    "KXLOWTLAX":   {"timezone": "America/Los_Angeles", "city": "Los Angeles",   "lat": 33.9425,  "lon": -118.4081, "temp_type": "low"},
    "KXLOWTLV":    {"timezone": "America/Los_Angeles", "city": "Las Vegas",     "lat": 36.0840,  "lon": -115.1537, "temp_type": "low"},
    "KXLOWTMIA":   {"timezone": "America/New_York",    "city": "Miami",         "lat": 25.7959,  "lon": -80.2870,  "temp_type": "low"},
    "KXLOWTMIN":   {"timezone": "America/Chicago",     "city": "Minneapolis",   "lat": 44.8848,  "lon": -93.2223,  "temp_type": "low"},
    "KXLOWTNOLA":  {"timezone": "America/Chicago",     "city": "New Orleans",   "lat": 29.9934,  "lon": -90.2580,  "temp_type": "low"},
    "KXLOWTNYC":   {"timezone": "America/New_York",    "city": "New York",      "lat": 40.6413,  "lon": -73.7781,  "temp_type": "low"},
    "KXLOWTOKC":   {"timezone": "America/Chicago",     "city": "Oklahoma City", "lat": 35.3931,  "lon": -97.6007,  "temp_type": "low"},
    "KXLOWTPHIL":  {"timezone": "America/New_York",    "city": "Philadelphia",  "lat": 39.8721,  "lon": -75.2411,  "temp_type": "low"},
    "KXLOWTSATX":  {"timezone": "America/Chicago",     "city": "San Antonio",   "lat": 29.5337,  "lon": -98.4698,  "temp_type": "low"},
    "KXLOWTSEA":   {"timezone": "America/Los_Angeles", "city": "Seattle",       "lat": 47.4502,  "lon": -122.3088, "temp_type": "low"},
    "KXLOWTSFO":   {"timezone": "America/Los_Angeles", "city": "San Francisco", "lat": 37.6213,  "lon": -122.3790, "temp_type": "low"},
}

for _cfg in SERIES_CONFIG.values():
    _cfg["enabled"] = True

if ENABLED_SERIES:
    enabled_set = {s.strip() for s in ENABLED_SERIES.split(",") if s.strip()}
    for series_ticker, cfg in SERIES_CONFIG.items():
        cfg["enabled"] = series_ticker in enabled_set
    log.info(
        f"ENABLED_SERIES override active — {len(enabled_set)} series enabled: "
        f"{sorted(enabled_set)}"
    )


def is_series_tradeable(series_ticker: str) -> bool:
    cfg = SERIES_CONFIG.get(series_ticker)
    if cfg is None:
        return False
    return cfg.get("enabled", True)


def is_series_watchlistable(series_ticker: str) -> bool:
    return series_ticker in SERIES_CONFIG

# ---------------------------------------------------------------------------
# Kalshi API auth
# ---------------------------------------------------------------------------
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


def kalshi_get(endpoint: str, params: dict = None):
    path = f"/trade-api/v2{endpoint}"
    url  = f"{KALSHI_BASE_URL}{endpoint}"
    r = requests.get(url, headers=_get_headers("GET", path), params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def kalshi_post(endpoint: str, body: dict):
    path = f"/trade-api/v2{endpoint}"
    url  = f"{KALSHI_BASE_URL}{endpoint}"
    r = requests.post(url, headers=_get_headers("POST", path), json=body, timeout=10)
    if not r.ok:
        log.error(f"kalshi_post {endpoint} failed [{r.status_code}]: {r.text}")
    r.raise_for_status()
    return r.json()


def cancel_order(order_id: str) -> bool:
    if not order_id or order_id == "UNKNOWN":
        return False
    try:
        endpoint = f"/portfolio/events/orders/{order_id}"
        path = f"/trade-api/v2{endpoint}"
        url = f"{KALSHI_BASE_URL}{endpoint}"
        r = requests.delete(url, headers=_get_headers("DELETE", path), timeout=10)
        if r.ok:
            return True
        log.error(f"cancel_order {order_id} failed [{r.status_code}]: {r.text}")
        return False
    except Exception as e:
        log.error(f"cancel_order {order_id} error: {e}")
        return False

# ---------------------------------------------------------------------------
# Open-Meteo GFS025 ensemble
# ---------------------------------------------------------------------------
def _percentile(values: list[float], pct: float) -> float:
    sorted_vals = sorted(values)
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (k - f) * (sorted_vals[c] - sorted_vals[f])


# ENSEMBLE — not used in market-based strategy
# def fetch_ensemble_snapshot(series_ticker: str) -> dict | None:
#     cfg = SERIES_CONFIG.get(series_ticker)
#     if not cfg:
#         log.error(f"Unknown series ticker: {series_ticker}")
#         return None
#
#     lat       = cfg["lat"]
#     lon       = cfg["lon"]
#     temp_type = cfg["temp_type"]
#     local_tz  = ZoneInfo(cfg["timezone"])
#     target_date = (datetime.now(tz=local_tz) + timedelta(days=1)).date()
#     end_date    = target_date + timedelta(days=1)
#
#     params = {
#         "latitude":   lat,
#         "longitude":  lon,
#         "models":     "gfs025",
#         "hourly":     "temperature_2m",
#         "start_date": target_date.isoformat(),
#         "end_date":   end_date.isoformat(),
#     }
#
#     try:
#         responses = _ensemble_client.weather_api(ENSEMBLE_API_URL, params=params)
#         response  = responses[0]
#         hourly    = response.Hourly()
#         start_ts  = hourly.Time()
#         interval  = hourly.Interval()
#         start_dt  = datetime.fromtimestamp(start_ts, tz=UTC)
#
#         member_values_f: list[float] = []
#         for idx in range(31):
#             temps_c = hourly.Variables(idx).ValuesAsNumpy().tolist()
#             values_for_date: list[float] = []
#             for i, temp_c in enumerate(temps_c):
#                 if temp_c is None:
#                     continue
#                 ts_dt = start_dt + timedelta(seconds=interval * i)
#                 if ts_dt.astimezone(local_tz).date() == target_date:
#                     values_for_date.append(float(temp_c))
#             if not values_for_date:
#                 continue
#             daily_c = max(values_for_date) if temp_type == "high" else min(values_for_date)
#             member_values_f.append((daily_c * 9 / 5) + 32)
#
#         if not member_values_f:
#             log.warning(f"No ensemble member values for {series_ticker} on {target_date}")
#             return None
#
#         return {
#             "series_ticker": series_ticker,
#             "temp_type":     temp_type,
#             "mean":          statistics.mean(member_values_f),
#             "p10":           _percentile(member_values_f, 10),
#             "p25":           _percentile(member_values_f, 25),
#             "p50":           _percentile(member_values_f, 50),
#             "p75":           _percentile(member_values_f, 75),
#             "p90":           _percentile(member_values_f, 90),
#             "raw_members":   json.dumps(member_values_f),
#             "snapshot_time": datetime.now(tz=UTC).isoformat(),
#         }
#     except Exception as e:
#         log.error(f"Ensemble fetch failed for {series_ticker}: {e}")
#         return None

# ---------------------------------------------------------------------------
# Bracket helpers
# ---------------------------------------------------------------------------
def _bracket_label(market: dict) -> str:
    return (market.get("yes_sub_title") or market.get("no_sub_title") or "").strip()


def _bracket_floor(label: str) -> float:
    label = label.strip()
    lower = label.lower()
    if "or below" in lower:
        return -999.0
    if "or above" in lower:
        return 999.0
    if " to " in lower:
        try:
            return float(label.split(" to ")[0].replace("°", "").strip())
        except ValueError:
            return 0.0
    try:
        return float(label.replace("°", "").split("-")[0])
    except (ValueError, IndexError):
        return 0.0


def identify_forecast_bracket(markets: list, forecast_temp: float) -> dict | None:
    """Return the market whose bracket contains forecast_temp."""
    rounded_temp = round(forecast_temp)
    for m in markets:
        s = _bracket_label(m)
        lower = s.lower()
        if "or below" in lower:
            ceiling = float(s.split("or below")[0].replace("°", "").strip()) + 1
            if rounded_temp < ceiling:
                return m
        elif "or above" in lower:
            lo = float(s.split("or above")[0].replace("°", "").strip())
            if rounded_temp >= lo:
                return m
        elif " to " in lower:
            parts = [p.replace("°", "").strip() for p in s.split(" to ")]
            if len(parts) == 2:
                try:
                    lo, hi = float(parts[0]), float(parts[1])
                    if lo <= rounded_temp <= hi:
                        return m
                except ValueError:
                    pass
    log.warning(f"No bracket matched for {forecast_temp}°F. Brackets tried: {[_bracket_label(m) for m in markets]}")
    return None


# ENSEMBLE — not used in market-based strategy
# def identify_top_brackets(series_ticker: str, member_values: list, markets: list) -> list:
#     bracket_counts: dict[str, dict] = {}
#     for val in member_values:
#         bracket = identify_forecast_bracket(markets, val)
#         if bracket is None:
#             continue
#         ticker = bracket["ticker"]
#         label  = _bracket_label(bracket)
#         if ticker not in bracket_counts:
#             bracket_counts[ticker] = {
#                 "ticker":        ticker,
#                 "bracket_label": label,
#                 "member_count":  0,
#             }
#         bracket_counts[ticker]["member_count"] += 1
#
#     ranked = sorted(bracket_counts.values(), key=lambda x: x["member_count"], reverse=True)
#     return ranked[:3]

# ---------------------------------------------------------------------------
# Market helpers & monitor cycle
# ---------------------------------------------------------------------------
def get_occurrence_datetime(market: dict) -> datetime | None:
    raw = market.get("occurrence_datetime")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (ValueError, TypeError):
        return None


def is_within_cutoff(occurrence_dt: datetime) -> bool:
    return datetime.now(tz=UTC) >= occurrence_dt - timedelta(hours=1)


def is_open_snapshot_window() -> bool:
    now = datetime.now(tz=UTC)
    return now.hour == 14 and 5 <= now.minute < 15


def _tomorrow_utc_date() -> str:
    return (datetime.now(tz=UTC) + timedelta(days=1)).date().isoformat()


def market_age_hours(occurrence_dt: datetime) -> float:
    market_open = occurrence_dt.replace(hour=14, minute=0, second=0, microsecond=0) - timedelta(days=1)
    age = (datetime.now(tz=UTC) - market_open).total_seconds() / 3600
    return max(0.0, age)


def get_yes_ask(market: dict) -> float | None:
    raw = market.get("yes_ask_dollars")
    if raw is None or raw == "":
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


def get_yes_buy_depth(market_ticker: str, sell_target: float) -> float:
    try:
        data = kalshi_get(f"/markets/{market_ticker}/orderbook")
        orderbook_fp = data.get("orderbook_fp") or {}
        no_dollars = orderbook_fp.get("no_dollars") or []
        max_no_price = 1.0 - sell_target
        total = 0.0
        for entry in no_dollars:
            if not entry or len(entry) < 2:
                continue
            no_price = float(entry[0])
            size = float(entry[1])
            if no_price <= max_no_price:
                total += size
        return total
    except Exception as e:
        log.warning(f"get_yes_buy_depth failed for {market_ticker}: {e}")
        return 0.0


def parse_event_date(market_ticker: str) -> str:
    parts = market_ticker.split("-")
    if len(parts) < 2:
        return "—"
    try:
        dt = datetime.strptime(parts[1].upper(), "%y%b%d")
        return f"{dt.strftime('%b')} {dt.day}"
    except ValueError:
        return "—"


def parse_series_from_ticker(market_ticker: str) -> str:
    if "-26" in market_ticker:
        return market_ticker.split("-26", 1)[0]
    return market_ticker.split("-", 1)[0]


# def get_vwap(market_ticker: str) -> tuple[float | None, int]:
#     cached = vwap_cache.get(market_ticker)
#     if cached:
#         age = datetime.now(tz=UTC) - cached["updated_at"]
#         if age < timedelta(minutes=VWAP_CACHE_TTL_MINUTES):
#             return cached["vwap"], cached["candles"]
#
#     series_ticker = parse_series_from_ticker(market_ticker)
#     now = datetime.now(tz=UTC)
#     end_ts = int(now.timestamp())
#     start_ts = int((now - timedelta(hours=2)).timestamp())
#
#     try:
#         data = kalshi_get(
#             f"/series/{series_ticker}/markets/{market_ticker}/candlesticks",
#             params={
#                 "period_interval": 60,
#                 "start_ts": start_ts,
#                 "end_ts": end_ts,
#             },
#         )
#         candlesticks = data.get("candlesticks", [])
#     except Exception as e:
#         log.warning(f"VWAP fetch failed for {market_ticker}: {e}")
#         vwap_cache[market_ticker] = {
#             "vwap": None,
#             "candles": 0,
#             "updated_at": datetime.now(tz=UTC),
#         }
#         return None, 0
#
#     pv_sum = 0.0
#     vol_sum = 0.0
#     candle_count = 0
#     for candle in candlesticks:
#         volume = float(candle.get("volume_fp", 0) or 0)
#         if volume <= 0:
#             continue
#         price_obj = candle.get("price") or {}
#         close_raw = price_obj.get("close_dollars") or price_obj.get("mean_dollars")
#         if close_raw is None or close_raw == "":
#             continue
#         close_price = float(close_raw)
#         pv_sum += close_price * volume
#         vol_sum += volume
#         candle_count += 1
#
#     vwap = (pv_sum / vol_sum) if vol_sum > 0 else None
#     vwap_cache[market_ticker] = {
#         "vwap": vwap,
#         "candles": candle_count,
#         "updated_at": datetime.now(tz=UTC),
#     }
#     return vwap, candle_count


def poll_order_fill(order_id: str, timeout_seconds: int = 30, interval_seconds: int = 3) -> dict | None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            data = kalshi_get(f"/portfolio/orders/{order_id}")
            order = data.get("order") or data
            status = order.get("status", "")
            if status == "executed":
                return order
            if status in ("cancelled", "expired", "canceled"):
                return None
        except Exception as e:
            log.error(f"poll_order_fill error for {order_id}: {e}")
        time.sleep(interval_seconds)
    log.warning(f"poll_order_fill timed out for {order_id} after {timeout_seconds}s")
    return None


def get_actual_fill_price(order: dict) -> float | None:
    if get_filled_quantity(order) == 0:
        return None
    raw = order.get("average_fill_price")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def get_filled_quantity(order: dict) -> int:
    raw = order.get("fill_count")
    if raw is None:
        return 0
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return 0


def get_polled_exit_price(order: dict) -> float | None:
    for key in ("yes_price_dollars", "average_fill_price"):
        raw = order.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def get_sell_target(entry_price: float) -> float:
    if entry_price <= 0.02:
        return TARGET_TIER_1
    if entry_price <= 0.04:
        return TARGET_TIER_2
    if entry_price <= 0.06:
        return TARGET_TIER_3
    if entry_price <= 0.09:
        return TARGET_TIER_4
    if entry_price <= 0.11:
        return TARGET_TIER_5
    return TARGET_TIER_6


def calc_taker_fee(price: float, contracts: int) -> float:
    return 0.07 * price * (1 - price) * contracts


def calc_maker_fee(price: float, contracts: int) -> float:
    return 0.07 * 0.25 * price * (1 - price) * contracts


def fetch_wallet_balance() -> float | None:
    try:
        data = kalshi_get("/portfolio/balance")
        for key in ("cash_balance", "available_balance", "balance"):
            raw = data.get(key)
            if raw is None:
                continue
            val = float(raw)
            if val > 1000.0:
                val = val / 100.0
            log.debug(f"fetch_wallet_balance: using key '{key}' = ${val:.2f}")
            return val
        return None
    except Exception as e:
        log.warning(f"fetch_wallet_balance failed: {e}")
        return None


def can_place_trade() -> bool:
    trade_cost = TRADE_AMOUNT_CENTS / 100

    if PAPER_TRADING:
        projected = paper_balance - cycle_spent - trade_cost
        if projected < MIN_WALLET_BALANCE:
            log.info(
                f"Paper wallet guard: paper_balance=${paper_balance:.2f} "
                f"cycle_spent=${cycle_spent:.2f} trade_cost=${trade_cost:.2f} "
                f"projected=${projected:.2f} — skipping"
            )
            return False
    else:
        balance = fetch_wallet_balance()
        if balance is None:
            log.warning("Wallet guard: could not fetch balance — skipping trade")
            return False

        projected_balance = balance - cycle_spent - trade_cost
        if projected_balance < MIN_WALLET_BALANCE:
            log.info(
                f"Wallet guard: balance=${balance:.2f} cycle_spent=${cycle_spent:.2f} "
                f"trade_cost=${trade_cost:.2f} projected=${projected_balance:.2f} — skipping"
            )
            return False

    conn = sqlite3.connect(DB_PATH)
    count = conn.execute("""
        SELECT COUNT(*) FROM trades
        WHERE exit_price IS NULL AND run_id = ?
    """, (current_run_id,)).fetchone()[0]
    conn.close()

    if count >= MAX_OPEN_POSITIONS:
        log.info(f"Position cap reached ({count}/{MAX_OPEN_POSITIONS}) — skipping")
        return False

    return True


def reset_cycle_spent() -> None:
    global cycle_spent
    cycle_spent = 0.0
    log.debug("Cycle spend tracker reset")


def should_top_up(ticker: str, open_time: str | None = None) -> tuple[bool, float]:
    occurrence_dt = None
    in_watchlist = False
    for info in watchlist.values():
        for bracket in info.get("brackets", []):
            if bracket.get("ticker") == ticker:
                in_watchlist = True
                occurrence_dt = info.get("occurrence_dt")
                break
        if in_watchlist:
            break

    if not in_watchlist:
        log.debug(
            f"Top-up blocked for {ticker} — not found in active watchlist"
        )
        return (False, 0.0)

    if occurrence_dt and is_within_cutoff(occurrence_dt):
        log.debug(
            f"Top-up blocked for {ticker} — within cutoff of occurrence {occurrence_dt}"
        )
        return (False, 0.0)

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT entry_price, volume FROM trades
        WHERE market_ticker = ? AND exit_price IS NULL AND run_id = ?
    """, (ticker, current_run_id)).fetchall()
    conn.close()

    existing_cost_basis = sum(
        (entry_price or 0) * (float(volume) if volume else 0)
        for entry_price, volume in rows
    )
    target_size = TRADE_AMOUNT_CENTS / 100
    remaining_budget = target_size - existing_cost_basis
    if remaining_budget < 0.50:
        return (False, 0.0)
    return (True, remaining_budget)


def place_trade(
    series_ticker: str,
    bracket: dict,
    market: dict,
    yes_ask: float,
    vwap: float | None = None,
    is_mean_bracket: bool = False,
    remaining_budget: float | None = None,
    open_time: str | None = None,
    rank: int | None = None,
) -> None:
    global cycle_spent, paper_balance

    if not can_place_trade():
        return

    ticker        = bracket["ticker"]
    bracket_label = bracket["bracket_label"]
    trade_dollars = TRADE_AMOUNT_CENTS / 100
    event_date    = parse_event_date(ticker)

    if PAPER_TRADING and ticker in open_positions:
        return

    if PAPER_TRADING:
        order_id = f"PAPER-{int(time.time())}"
        yes_bid       = float(market.get("yes_bid_dollars", 0) or 0)
        open_interest = float(market.get("open_interest_fp", 0) or 0)
        contracts  = int(trade_dollars / yes_ask)
        entry_fee  = calc_taker_fee(yes_ask, contracts)
        slippage   = 0.0
        log.info(
            f"[PAPER] BUY {series_ticker} {bracket_label} @ {yes_ask:.2f} "
            f"| holding to settlement | qty {contracts}"
        )
        price_2h, price_4h, price_6h = get_pre_entry_prices(
            ticker, datetime.now(tz=UTC).isoformat()
        )
        if record_trade({
            "series_ticker": series_ticker,
            "market_ticker": ticker,
            "bracket_label": bracket_label,
            "side":          "YES",
            "entry_price":   yes_ask,
            "sell_target":   0.00,
            "order_id":      order_id,
            "volume":        contracts,
            "yes_bid":       yes_bid,
            "open_interest": open_interest,
            "vwap":          vwap,
            "is_mean_bracket": 1 if is_mean_bracket else 0,
            "entry_fee":     entry_fee,
            "slippage":      slippage,
            "price_2h_before_entry": price_2h,
            "price_4h_before_entry": price_4h,
            "price_6h_before_entry": price_6h,
        }):
            paper_balance -= TRADE_AMOUNT_CENTS / 100
        open_positions[ticker] = {
            "entry":    yes_ask,
            "target":   0.00,
            "order_id": order_id,
        }
        send_telegram(
            f"💰 Trade placed [PAPER]\n"
            f"{series_ticker} | {event_date} | {bracket_label}\n"
            f"Rank: #{rank} | Entry: ${yes_ask:.2f} | Qty: {contracts} "
            f"(${contracts * yes_ask:.2f}) | Holding to settlement"
        )
        return

    try:
        buy_dollars = remaining_budget if remaining_budget is not None else trade_dollars
        contracts_to_buy = buy_dollars / yes_ask

        conn = sqlite3.connect(DB_PATH)
        existing_rows = conn.execute("""
            SELECT id, entry_price, volume, sell_order_id, entry_fee, sell_target
            FROM trades
            WHERE market_ticker = ? AND exit_price IS NULL AND run_id = ?
            ORDER BY id DESC
        """, (ticker, current_run_id)).fetchall()
        conn.close()

        buy_resp = kalshi_post("/portfolio/events/orders", {
            "ticker":                     ticker,
            "client_order_id":            str(uuid.uuid4()),
            "side":                       "bid",
            "count":                      f"{contracts_to_buy:.2f}",
            "price":                      f"{yes_ask:.4f}",
            "time_in_force":              "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
        })
        order_id = buy_resp.get("order_id", "UNKNOWN")
        filled_qty = get_filled_quantity(buy_resp)
        if filled_qty == 0:
            log.info(f"IOC buy not filled for {ticker} at {yes_ask:.2f} — price moved, skipping")
            return
        actual_fill_price = get_actual_fill_price(buy_resp)
        if actual_fill_price is None:
            log.warning(f"Could not extract fill price for {ticker}, falling back to yes_ask")
            actual_fill_price = yes_ask

        if existing_rows:
            existing_cost_basis = sum(
                (row[1] or 0) * (float(row[2]) if row[2] else 0)
                for row in existing_rows
            )
            existing_total_contracts = sum(
                float(row[2]) if row[2] else 0 for row in existing_rows
            )
            new_total_contracts = existing_total_contracts + filled_qty
            new_blended_entry = (
                (existing_cost_basis + (filled_qty * actual_fill_price)) / new_total_contracts
            )
            log.info(
                f"[LIVE] TOP-UP BUY {series_ticker} {bracket_label} @ {actual_fill_price:.2f} "
                f"| order {order_id} | holding to settlement"
            )
        else:
            log.info(
                f"[LIVE] BUY {series_ticker} {bracket_label} @ {actual_fill_price:.2f} "
                f"| order {order_id} | holding to settlement | qty {filled_qty}"
            )
    except Exception as e:
        log.error(f"Order failed for {ticker}: {e}")
        return

    yes_bid       = float(market.get("yes_bid_dollars", 0) or 0)
    open_interest = float(market.get("open_interest_fp", 0) or 0)
    incremental_entry_fee = calc_taker_fee(actual_fill_price, filled_qty)
    slippage      = actual_fill_price - yes_ask
    target_position_size = trade_dollars

    if existing_rows:
        trade_id = existing_rows[0][0]
        prior_entry_fee = existing_rows[0][4] or 0.0
        cumulative_entry_fee = prior_entry_fee + incremental_entry_fee
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            UPDATE trades
            SET entry_price = ?, sell_target = ?, volume = ?, sell_order_id = NULL, entry_fee = ?
            WHERE id = ?
        """, (
            new_blended_entry, 0.00, new_total_contracts,
            cumulative_entry_fee, trade_id,
        ))
        conn.commit()
        conn.close()
        cycle_spent += filled_qty * actual_fill_price
        open_positions[ticker] = {
            "entry":    new_blended_entry,
            "target":   0.00,
            "order_id": order_id,
        }
        send_telegram(
            f"🔼 Topped up [LIVE]\n"
            f"{series_ticker} | {event_date} | {bracket_label}\n"
            f"New blended entry: ${new_blended_entry:.3f} | "
            f"Total qty: {new_total_contracts:.0f} (${new_total_contracts * new_blended_entry:.2f} of "
            f"${target_position_size:.2f} target) | Holding to settlement"
        )
        return

    price_2h, price_4h, price_6h = get_pre_entry_prices(
        ticker, datetime.now(tz=UTC).isoformat()
    )
    if record_trade({
        "series_ticker": series_ticker,
        "market_ticker": ticker,
        "bracket_label": bracket_label,
        "side":          "YES",
        "entry_price":   actual_fill_price,
        "sell_target":   0.00,
        "order_id":      order_id,
        "sell_order_id": None,
        "volume":        filled_qty,
        "yes_bid":       yes_bid,
        "open_interest": open_interest,
        "vwap":          vwap,
        "is_mean_bracket": 1 if is_mean_bracket else 0,
        "entry_fee":     incremental_entry_fee,
        "slippage":      slippage,
        "price_2h_before_entry": price_2h,
        "price_4h_before_entry": price_4h,
        "price_6h_before_entry": price_6h,
    }):
        cycle_spent += trade_dollars
    open_positions[ticker] = {
        "entry":    actual_fill_price,
        "target":   0.00,
        "order_id": order_id,
    }
    send_telegram(
        f"💰 Trade placed [LIVE]\n"
        f"{series_ticker} | {event_date} | {bracket_label}\n"
        f"Rank: #{rank} | Entry: ${actual_fill_price:.2f} | Qty: {filled_qty} "
        f"(${filled_qty * actual_fill_price:.2f}) | Holding to settlement"
    )



def is_valid_top3(scored: list[tuple[float, dict]]) -> bool:
    if not scored:
        return False
    top_price = scored[0][0]
    count_top = sum(1 for price, _ in scored if price == top_price)
    if count_top >= 4:
        return False
    if count_top == 3:
        return True
    second_price = None
    for price, _ in scored:
        if price != top_price:
            second_price = price
            break
    if second_price is None:
        return True
    count_second = sum(1 for price, _ in scored if price == second_price)
    if count_second >= 3:
        return False
    return True


def build_watchlist() -> dict:
    global watchlist
    new_watchlist: dict = {}
    tomorrow_utc = (datetime.now(tz=UTC) + timedelta(days=1)).date()

    for series_ticker, cfg in SERIES_CONFIG.items():
        if not is_series_watchlistable(series_ticker):
            continue

        local_tz    = ZoneInfo(cfg["timezone"])
        tomorrow    = (datetime.now(tz=local_tz) + timedelta(days=1)).date()
        date_suffix = tomorrow.strftime("%y%b%d").upper()
        event_ticker = f"{series_ticker}-{date_suffix}"

        try:
            data = kalshi_get(
                "/markets",
                params={"series_ticker": series_ticker, "status": "open", "limit": 100},
            )
            markets = [m for m in data.get("markets", []) if m.get("event_ticker") == event_ticker]
        except Exception as e:
            log.warning(f"{series_ticker}: market fetch failed: {e}")
            time.sleep(0.25)
            continue

        if len(markets) < 2:
            log.info(f"{series_ticker}: fewer than 2 markets for {event_ticker}, skipping")
            time.sleep(0.25)
            continue

        scored: list[tuple[float, dict]] = []
        for m in markets:
            raw = m.get("yes_ask_dollars")
            if raw is None:
                continue
            try:
                ask = float(raw)
            except (TypeError, ValueError):
                continue
            if ask == 0.00:
                continue
            scored.append((ask, m))

        if len(scored) < 2:
            log.info(f"{series_ticker}: fewer than 2 priced markets for {event_ticker}, skipping")
            time.sleep(0.25)
            continue

        scored.sort(key=lambda x: x[0], reverse=True)
        if not is_valid_top3(scored):
            log.warning(f"{series_ticker}: prices not differentiated yet, will retry")
            time.sleep(0.25)
            continue

        top = scored[:WATCHLIST_SIZE]
        occurrence_dt = get_occurrence_datetime(top[0][1])

        brackets = []
        for rank, (ask, m) in enumerate(top, start=1):
            brackets.append({
                "ticker":          m["ticker"],
                "bracket_label":   _bracket_label(m),
                "rank":            rank,
                "yes_ask_at_open": ask,
                "open_time":       m.get("open_time") or "",
            })

        new_watchlist[event_ticker] = {
            "series_ticker": series_ticker,
            "event_date":    tomorrow.isoformat(),
            "occurrence_dt": occurrence_dt,
            "brackets":      brackets,
        }
        time.sleep(0.25)

    watchlist = new_watchlist
    n_events = len(watchlist)
    total_brackets = sum(len(v["brackets"]) for v in watchlist.values())
    log.info(f"Watchlist built: {n_events} events, {total_brackets} brackets total")
    send_telegram(
        f"📋 Watchlist built for {tomorrow_utc.isoformat()}\n"
        f"{n_events} events | {total_brackets} brackets watching"
    )
    save_watchlist_to_db(watchlist)
    return watchlist


def _enabled_series_in_watchlist(wl: dict) -> set[str]:
    return {info["series_ticker"] for info in wl.values()}


def try_build_watchlist() -> bool:
    wl = build_watchlist()
    enabled = {s for s, c in SERIES_CONFIG.items() if c.get("enabled", True)}
    missing = enabled - _enabled_series_in_watchlist(wl)
    now = datetime.now(tz=UTC)

    if missing and now.hour < 16:
        n = len(missing)
        log.info(f"{n} series still pending watchlist selection")
        send_telegram(
            f"⏳ Watchlist incomplete: {n} series prices not differentiated yet, retrying in 5 min"
        )
        return False

    if missing:
        n = len(missing)
        log.warning(
            f"Watchlist finalized with {n} series skipped (prices never differentiated): "
            f"{', '.join(sorted(missing))}"
        )
        send_telegram(
            f"⚠️ Watchlist finalized with {n} series skipped (prices never differentiated)"
        )

    return True


def run_watchlist_monitor() -> None:
    global last_cycle_at, trading_paused

    if trading_paused:
        log.info("Trading paused — skipping monitor cycle")
        last_cycle_at = datetime.now(tz=UTC)
        return

    if not watchlist_is_valid:
        return

    for event_ticker, info in watchlist.items():
        series_ticker = info.get("series_ticker", "")
        if not is_series_tradeable(series_ticker):
            continue

        occurrence_dt = info.get("occurrence_dt")
        brackets = info.get("brackets", [])
        if occurrence_dt and is_within_cutoff(occurrence_dt):
            event_date = info.get("event_date", "—")
            log.info(
                f"{series_ticker} | {event_date} | all — skipping, "
                f"within 1h cutoff (occurrence={occurrence_dt})"
            )
            continue

        for bracket in brackets:
            if bracket.get("rank", 1) > WATCHLIST_SIZE:
                continue
            ticker        = bracket["ticker"]
            bracket_label = bracket["bracket_label"]
            rank          = bracket["rank"]
            event_date    = parse_event_date(ticker)
            try:
                data    = kalshi_get(f"/markets/{ticker}")
                market  = data.get("market", {})
                yes_ask = get_yes_ask(market)
            except Exception as e:
                log.warning(f"Market fetch failed for {ticker}: {e}")
                continue

            if yes_ask is not None and yes_ask <= ENTRY_CAP:
                if yes_ask < ENTRY_FLOOR:
                    log.info(
                        f"{series_ticker} | {event_date} | {bracket_label} — skipping, "
                        f"yes_ask={yes_ask:.2f} below floor"
                    )
                    continue
                yes_bid = float(market.get("yes_bid_dollars", 0) or 0)
                if yes_bid == 0.00:
                    log.info(
                        f"{series_ticker} | {event_date} | {bracket_label} — skipping, "
                        f"yes_bid=0.00 (no buyers)"
                    )
                    continue
                if yes_ask - yes_bid > MAX_SPREAD:
                    log.info(
                        f"{series_ticker} | {event_date} | {bracket_label} — skipping, spread too wide"
                    )
                    continue
                open_time = bracket.get("open_time") or market.get("open_time")
                if PAPER_TRADING:
                    if ticker in open_positions:
                        log.info(
                            f"{series_ticker} | {event_date} | {bracket_label} — skipping, "
                            f"already have position"
                        )
                        continue
                    remaining_budget = None
                else:
                    can_top_up, remaining_budget = should_top_up(ticker, open_time)
                    if not can_top_up:
                        log.info(
                            f"{series_ticker} | {event_date} | {bracket_label} — skipping, "
                            f"already at target size"
                        )
                        continue

                log.info(
                    f"ENTRY SIGNAL [rank {rank}]: {series_ticker} | {event_date} | "
                    f"{bracket_label} yes_ask={yes_ask:.2f} bid={yes_bid:.2f}"
                )
                place_trade(
                    series_ticker,
                    bracket,
                    market,
                    yes_ask,
                    vwap=None,
                    is_mean_bracket=(rank == 1),
                    remaining_budget=remaining_budget,
                    open_time=open_time,
                    rank=bracket.get("rank"),
                )
            else:
                ask_str = f"{yes_ask:.2f}" if yes_ask is not None else "N/A"
                log.debug(
                    f"{series_ticker} | {event_date} | {bracket_label} yes_ask={ask_str} — no dip"
                )

    last_cycle_at = datetime.now(tz=UTC)


def record_price_snapshot(
    market_ticker: str,
    series_ticker: str,
    temp_type: str,
    yes_ask: float | None,
    yes_bid: float | None,
    last_price: float | None,
) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            INSERT INTO price_history
                (market_ticker, series_ticker, temp_type, yes_ask, yes_bid, last_price, run_id)
            VALUES (?,?,?,?,?,?,?)
        """, (
            market_ticker, series_ticker, temp_type,
            yes_ask, yes_bid, last_price, current_run_id,
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug(f"record_price_snapshot failed for {market_ticker}: {e}")


def run_price_history_capture() -> None:
    if not watchlist_is_valid or not watchlist:
        return

    count = 0
    for info in watchlist.values():
        series_ticker = info.get("series_ticker", "")
        temp_type = "HIGH" if "HIGH" in series_ticker else "LOW"
        for bracket in info.get("brackets", []):
            ticker = bracket["ticker"]
            try:
                data = kalshi_get(f"/markets/{ticker}")
                market = data.get("market", {})
            except Exception as e:
                log.debug(f"price history fetch failed for {ticker}: {e}")
                time.sleep(0.25)
                continue

            yes_ask = get_yes_ask(market)
            yes_bid_raw = market.get("yes_bid_dollars")
            try:
                yes_bid = float(yes_bid_raw) if yes_bid_raw not in (None, "") else None
            except (TypeError, ValueError):
                yes_bid = None
            last_price_raw = market.get("last_price_dollars")
            try:
                last_price = float(last_price_raw) if last_price_raw not in (None, "") else None
            except (TypeError, ValueError):
                last_price = None

            record_price_snapshot(
                ticker, series_ticker, temp_type, yes_ask, yes_bid, last_price,
            )
            count += 1
            time.sleep(0.25)

    log.info(f"Price history: recorded {count} snapshots")


# ENSEMBLE — not used in market-based strategy
# def run_ensemble_update() -> dict:
#     watched: dict = {}
#     for series_ticker, cfg in SERIES_CONFIG.items():
#         if not cfg.get("enabled", True):
#             continue
#         snapshot = fetch_ensemble_snapshot(series_ticker)
#         if not snapshot:
#             log.warning(f"{series_ticker}: ensemble snapshot failed, skipping")
#             time.sleep(0.5)
#             continue
#
#         local_tz    = ZoneInfo(cfg["timezone"])
#         tomorrow    = (datetime.now(tz=local_tz) + timedelta(days=1)).date()
#         date_suffix = tomorrow.strftime("%y%b%d").upper()
#         event_ticker = f"{series_ticker}-{date_suffix}"
#
#         try:
#             data = kalshi_get("/markets", params={"series_ticker": series_ticker, "status": "open", "limit": 100})
#             markets = [m for m in data.get("markets", []) if m.get("event_ticker") == event_ticker]
#         except Exception as e:
#             log.warning(f"{series_ticker}: market fetch failed: {e}")
#             watched[series_ticker] = {
#                 "top_brackets":  [],
#                 "occurrence_dt": None,
#                 "raw_members":   json.loads(snapshot["raw_members"]),
#             }
#             time.sleep(0.5)
#             continue
#
#         if not markets:
#             member_values = json.loads(snapshot["raw_members"])
#             watched[series_ticker] = {
#                 "top_brackets":  [],
#                 "occurrence_dt": None,
#                 "raw_members":   member_values,
#             }
#             log.warning(f"{series_ticker}: no markets for {event_ticker}")
#             time.sleep(0.5)
#             continue
#
#         member_values = json.loads(snapshot["raw_members"])
#         top = identify_top_brackets(series_ticker, member_values, markets)[:2]
#         occurrence_dt = get_occurrence_datetime(markets[0])
#
#         watched[series_ticker] = {
#             "top_brackets":  top,
#             "occurrence_dt": occurrence_dt,
#             "raw_members":   member_values,
#         }
#         top_summary = ", ".join(f"{b['bracket_label']}({b['member_count']})" for b in top)
#         log.info(f"{series_ticker}: mean={snapshot['mean']:.1f}°F | top2: {top_summary}")
#         time.sleep(0.5)
#
#     send_telegram(f"🔄 Ensemble updated — watching {len(watched)} series")
#     return watched


# ENSEMBLE — not used in market-based strategy
# def refresh_markets() -> None:
#     global watched
#     updated = 0
#     for series_ticker, cfg in SERIES_CONFIG.items():
#         info = watched.get(series_ticker)
#         if not info or not info.get("raw_members"):
#             continue
#         if info.get("top_brackets"):
#             continue
#
#         local_tz     = ZoneInfo(cfg["timezone"])
#         tomorrow     = (datetime.now(tz=local_tz) + timedelta(days=1)).date()
#         date_suffix  = tomorrow.strftime("%y%b%d").upper()
#         event_ticker = f"{series_ticker}-{date_suffix}"
#
#         try:
#             data = kalshi_get("/markets", params={"series_ticker": series_ticker, "status": "open", "limit": 100})
#             markets = [m for m in data.get("markets", []) if m.get("event_ticker") == event_ticker]
#         except Exception as e:
#             log.warning(f"{series_ticker}: market refresh failed: {e}")
#             time.sleep(0.5)
#             continue
#
#         if not markets:
#             log.warning(f"{series_ticker}: still no markets for {event_ticker}")
#             time.sleep(0.5)
#             continue
#
#         member_values = info["raw_members"]
#         top = identify_top_brackets(series_ticker, member_values, markets)[:2]
#         occurrence_dt = get_occurrence_datetime(markets[0])
#         watched[series_ticker]["top_brackets"] = top
#         watched[series_ticker]["occurrence_dt"] = occurrence_dt
#         updated += 1
#         top_summary = ", ".join(f"{b['bracket_label']}({b['member_count']})" for b in top)
#         log.info(f"{series_ticker}: refreshed | top2: {top_summary}")
#         time.sleep(0.5)
#
#     log.info(f"Market refresh complete — {updated} series updated")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            series_ticker   TEXT NOT NULL,
            market_ticker   TEXT NOT NULL,
            bracket_label   TEXT NOT NULL,
            side            TEXT NOT NULL DEFAULT 'YES',
            entry_price     REAL NOT NULL,
            sell_target     REAL NOT NULL,
            exit_price      REAL,
            exit_reason     TEXT,
            order_id        TEXT,
            paper           INTEGER NOT NULL DEFAULT 1,
            volume          REAL,
            run_id          INTEGER,
            yes_bid         REAL,
            open_interest   REAL,
            vwap            REAL,
            is_mean_bracket INTEGER,
            created_at      TEXT DEFAULT (datetime('now')),
            closed_at       TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paper_runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at  TEXT DEFAULT (datetime('now')),
            note        TEXT
        )
    """)
    expected_columns = {
        "volume":        "REAL",
        "run_id":        "INTEGER",
        "yes_bid":       "REAL",
        "open_interest": "REAL",
        "vwap":          "REAL",
        "is_mean_bracket": "INTEGER",
        "sell_order_id":   "TEXT",
        "entry_fee":       "REAL",
        "exit_fee":        "REAL",
        "slippage":        "REAL",
        "last_known_fill_count": "REAL",
        "realized_pnl":        "REAL",
        "price_2h_before_entry": "REAL",
        "price_4h_before_entry": "REAL",
        "price_6h_before_entry": "REAL",
    }
    existing = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}
    for col, col_type in expected_columns.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {col_type}")
            log.info(f"Migrated trades table: added column {col}")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            series_ticker   TEXT NOT NULL,
            temp_type       TEXT NOT NULL,
            snapshot_time   TEXT NOT NULL,
            mean            REAL,
            p10             REAL,
            p25             REAL,
            p50             REAL,
            p75             REAL,
            p90             REAL,
            raw_members     TEXT,
            top_bracket_1   TEXT,
            top_bracket_2   TEXT,
            top_bracket_3   TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            event_ticker    TEXT NOT NULL,
            series_ticker   TEXT NOT NULL,
            event_date      TEXT NOT NULL,
            occurrence_dt   TEXT NOT NULL,
            bracket_ticker  TEXT NOT NULL,
            bracket_label   TEXT NOT NULL,
            rank            INTEGER NOT NULL,
            yes_ask_at_open REAL NOT NULL,
            open_time       TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        )
    """)
    watchlist_expected_columns = {
        "open_time": "TEXT",
    }
    watchlist_existing = {row[1] for row in conn.execute("PRAGMA table_info(watchlist)")}
    for col, col_type in watchlist_expected_columns.items():
        if col not in watchlist_existing:
            conn.execute(f"ALTER TABLE watchlist ADD COLUMN {col} {col_type}")
            log.info(f"Migrated watchlist table: added column {col}")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            market_ticker   TEXT NOT NULL,
            series_ticker   TEXT NOT NULL,
            temp_type       TEXT NOT NULL,
            yes_ask         REAL,
            yes_bid         REAL,
            last_price      REAL,
            observed_at     TEXT DEFAULT (datetime('now')),
            run_id          INTEGER
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_price_history_ticker_time
        ON price_history (market_ticker, observed_at)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def get_state(key: str, default: str = "") -> str:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT value FROM bot_state WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default


def set_state(key: str, value: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?,?)",
        (key, value),
    )
    conn.commit()
    conn.close()


def save_watchlist_to_db(watchlist: dict) -> None:
    tomorrow = _tomorrow_utc_date()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM watchlist WHERE event_date = ?", (tomorrow,))
    for event_ticker, info in watchlist.items():
        occurrence_dt = info.get("occurrence_dt")
        occ_str = occurrence_dt.isoformat() if occurrence_dt else ""
        for bracket in info.get("brackets", []):
            conn.execute("""
                INSERT INTO watchlist
                    (event_ticker, series_ticker, event_date, occurrence_dt,
                     bracket_ticker, bracket_label, rank, yes_ask_at_open, open_time)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                event_ticker,
                info["series_ticker"],
                info["event_date"],
                occ_str,
                bracket["ticker"],
                bracket["bracket_label"],
                bracket["rank"],
                bracket.get("yes_ask_at_open", 0.0),
                bracket.get("open_time", ""),
            ))
    conn.commit()
    conn.close()


def load_watchlist_from_db() -> dict:
    today = datetime.now(tz=UTC).date().isoformat()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT event_ticker, series_ticker, event_date, occurrence_dt,
               bracket_ticker, bracket_label, rank, yes_ask_at_open, open_time
        FROM watchlist
        WHERE event_date >= ?
        ORDER BY event_ticker, rank
    """, (today,)).fetchall()
    conn.close()

    if not rows:
        return {}

    result: dict = {}
    for (
        event_ticker, series_ticker, event_date, occurrence_dt,
        bracket_ticker, bracket_label, rank, yes_ask_at_open, open_time,
    ) in rows:
        if event_ticker not in result:
            occ_dt = None
            if occurrence_dt:
                try:
                    occ_dt = datetime.fromisoformat(str(occurrence_dt).replace("Z", "+00:00"))
                    if occ_dt.tzinfo is None:
                        occ_dt = occ_dt.replace(tzinfo=UTC)
                    occ_dt = occ_dt.astimezone(UTC)
                except (ValueError, TypeError):
                    pass
            result[event_ticker] = {
                "series_ticker": series_ticker,
                "event_date":    event_date,
                "occurrence_dt": occ_dt,
                "brackets":      [],
            }
        result[event_ticker]["brackets"].append({
            "ticker":          bracket_ticker,
            "bracket_label":   bracket_label,
            "rank":            rank,
            "yes_ask_at_open": yes_ask_at_open,
            "open_time":       open_time or "",
        })
    return result


def start_new_run(note: str = "") -> int:
    global current_run_id
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("INSERT INTO paper_runs (note) VALUES (?)", (note or None,))
    conn.commit()
    run_id = cur.lastrowid
    conn.close()
    current_run_id = run_id
    return run_id


def get_or_create_run() -> int:
    global current_run_id
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT MAX(id) FROM paper_runs").fetchone()
    conn.close()
    if row and row[0] is not None:
        current_run_id = row[0]
        log.info(f"Resuming paper run #{current_run_id}")
        send_telegram(f"♻️ Bot restarted — resuming paper run #{current_run_id}")
        return current_run_id
    run_id = start_new_run()
    log.info(f"Starting paper run #{run_id}")
    send_telegram(f"🆕 Paper run #{run_id} started")
    return run_id


def get_pre_entry_prices(
    market_ticker: str, entry_time: str
) -> tuple[float | None, float | None, float | None]:
    conn = sqlite3.connect(DB_PATH)
    prices: list[float | None] = []
    for hours in (2, 4, 6):
        row = conn.execute(
            f"""
            SELECT last_price FROM price_history
            WHERE market_ticker = ?
              AND observed_at <= datetime(?, '-{hours} hours')
            ORDER BY observed_at DESC LIMIT 1
            """,
            (market_ticker, entry_time),
        ).fetchone()
        prices.append(row[0] if row else None)
    conn.close()
    return prices[0], prices[1], prices[2]


def record_trade(trade: dict) -> int:
    conn = sqlite3.connect(DB_PATH)
    existing = conn.execute("""
        SELECT COUNT(*) FROM trades
        WHERE market_ticker = ? AND exit_price IS NULL AND run_id = ?
    """, (trade["market_ticker"], current_run_id)).fetchone()[0]
    if existing > 0:
        log.warning(
            f"Duplicate guard: already have open position for {trade['market_ticker']}, skipping insert"
        )
        conn.close()
        return 0

    cur = conn.execute("""
        INSERT INTO trades
            (series_ticker, market_ticker, bracket_label, side,
             entry_price, sell_target, order_id, paper, volume, run_id, yes_bid, open_interest, vwap, is_mean_bracket, sell_order_id, entry_fee, exit_fee, slippage,
             price_2h_before_entry, price_4h_before_entry, price_6h_before_entry)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        trade["series_ticker"],
        trade["market_ticker"],
        trade["bracket_label"],
        trade["side"],
        trade["entry_price"],
        trade["sell_target"],
        trade["order_id"],
        1 if PAPER_TRADING else 0,
        trade.get("volume"),
        current_run_id,
        trade.get("yes_bid"),
        trade.get("open_interest"),
        trade.get("vwap"),
        trade.get("is_mean_bracket", 0),
        trade.get("sell_order_id"),
        trade.get("entry_fee"),
        trade.get("exit_fee"),
        trade.get("slippage"),
        trade.get("price_2h_before_entry"),
        trade.get("price_4h_before_entry"),
        trade.get("price_6h_before_entry"),
    ))
    conn.commit()
    rowid = cur.lastrowid
    conn.close()
    return rowid


def close_trade(trade_id: int, exit_price: float, exit_reason: str, exit_fee: float | None = None):
    conn = sqlite3.connect(DB_PATH)
    if exit_fee is not None:
        conn.execute("""
            UPDATE trades SET exit_price=?, exit_reason=?, exit_fee=?, closed_at=datetime('now')
            WHERE id=?
        """, (exit_price, exit_reason, exit_fee, trade_id))
    else:
        conn.execute("""
            UPDATE trades SET exit_price=?, exit_reason=?, closed_at=datetime('now')
            WHERE id=?
        """, (exit_price, exit_reason, trade_id))
    conn.commit()
    conn.close()


def load_open_positions() -> dict:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT market_ticker, entry_price, sell_target, order_id
        FROM trades
        WHERE exit_price IS NULL AND run_id = ?
    """, (current_run_id,)).fetchall()
    conn.close()
    positions: dict = {}
    for market_ticker, entry_price, sell_target, order_id in rows:
        positions[market_ticker] = {
            "entry":    entry_price,
            "target":   sell_target,
            "order_id": order_id,
        }
    return positions


def reconcile_positions() -> None:
    global open_positions
    try:
        data = kalshi_get("/portfolio/positions", params={"limit": 100})
    except Exception as e:
        log.error(f"Reconcile: failed to fetch Kalshi positions: {e}")
        return

    positions = data.get("positions") or []
    if not positions:
        positions = data.get("market_positions") or []

    kalshi_open: set[str] = set()
    for pos in positions:
        try:
            count = float(pos.get("position_fp", 0) or 0)
        except (TypeError, ValueError):
            count = 0.0
        if count <= 0:
            continue
        ticker = pos.get("ticker") or pos.get("market_ticker")
        if ticker:
            kalshi_open.add(ticker)

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT id, market_ticker
        FROM trades
        WHERE exit_price IS NULL AND run_id = ?
    """, (current_run_id,)).fetchall()
    conn.close()

    confirmed = 0
    resolved = 0
    discrepancies = 0

    for trade_id, market_ticker in rows:
        if market_ticker in kalshi_open:
            log.debug(f"Reconcile: {market_ticker} confirmed open in Kalshi portfolio")
            confirmed += 1
            continue

        discrepancies += 1
        log.warning(
            f"Reconcile: {market_ticker} not found in Kalshi portfolio — "
            f"may have settled or been closed externally"
        )
        try:
            mdata  = kalshi_get(f"/markets/{market_ticker}")
            market = mdata.get("market", {})
        except Exception as e:
            log.warning(f"Reconcile: market fetch failed for {market_ticker}: {e}")
            time.sleep(0.25)
            continue

        status = market.get("status", "")
        if status in ("settled", "finalized"):
            win        = market.get("result", "") == "yes"
            exit_price = 1.00 if win else 0.00
            close_trade(trade_id, exit_price, "RECONCILED_SETTLEMENT")
            open_positions.pop(market_ticker, None)
            resolved += 1
            send_telegram(f"🔁 Reconciled: {market_ticker} already settled")
        else:
            log.warning(
                f"Reconcile: {market_ticker} status={status} — leaving open for manual review"
            )

        time.sleep(0.25)

    log.info(
        f"Reconciliation complete — {confirmed} positions confirmed, "
        f"{resolved} discrepancies resolved"
    )
    if discrepancies > 0:
        send_telegram(
            f"🔁 Reconciliation: {confirmed} confirmed, {discrepancies} discrepancies "
            f"({resolved} resolved)"
        )


def check_fills() -> None:
    global paper_balance

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT id, series_ticker, market_ticker, bracket_label, entry_price, entry_fee, volume, paper
        FROM trades
        WHERE exit_price IS NULL AND run_id = ?
    """, (current_run_id,)).fetchall()
    conn.close()

    for trade_id, series_ticker, market_ticker, bracket_label, entry_price, entry_fee, volume, paper in rows:
        entry_fee = entry_fee or 0.0
        try:
            data   = kalshi_get(f"/markets/{market_ticker}")
            market = data.get("market", {})
        except Exception as e:
            log.warning(f"check_fills: fetch failed for {market_ticker}: {e}")
            time.sleep(0.25)
            continue

        status = market.get("status", "")
        mode_label = "PAPER" if paper else "LIVE"
        event_date = parse_event_date(market_ticker)
        if status in ("settled", "finalized"):
            win        = market.get("result", "") == "yes"
            exit_price = 1.00 if win else 0.00
            exit_reason = "SETTLED_WIN" if win else "SETTLED_LOSS"
            exit_fee = calc_maker_fee(exit_price, _fee_contract_count(volume, entry_price))
            close_trade(trade_id, exit_price, exit_reason, exit_fee=exit_fee)
            open_positions.pop(market_ticker, None)
            if paper and win:
                contracts = float(volume) if volume else 0.0
                paper_balance += exit_price * contracts
            pnl = _trade_pnl(
                entry_price, exit_price,
                entry_fee=entry_fee, exit_fee=exit_fee,
                contracts=_contracts_from_volume(volume),
            )
            if win:
                send_telegram(
                    f"✅ Settled WIN [{mode_label}]\n"
                    f"{series_ticker} | {event_date} | {bracket_label}\n"
                    f"PnL: +${pnl:.2f}"
                )
            else:
                send_telegram(
                    f"❌ Settled LOSS [{mode_label}]\n"
                    f"{series_ticker} | {event_date} | {bracket_label}\n"
                    f"PnL: -${abs(pnl):.2f}"
                )

        time.sleep(0.25)



# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def check_health() -> None:
    if last_cycle_at is None:
        return

    age_minutes = (datetime.now(tz=UTC) - last_cycle_at).total_seconds() / 60
    if age_minutes > 10:
        msg = (
            f"⚠️ Health check: bot has not completed a monitor cycle "
            f"in {int(age_minutes)} minutes — may be stuck or crashed"
        )
        log.warning(msg)
        send_telegram(msg)


def send_daily_summary() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    recent_wins = conn.execute("""
        SELECT entry_price, exit_price, entry_fee, exit_fee, volume
        FROM trades
        WHERE exit_reason = 'SETTLED_WIN'
          AND closed_at >= datetime('now', '-24 hours')
          AND run_id = ? AND paper = 1
    """, (current_run_id,)).fetchall()

    recent_losses = conn.execute("""
        SELECT entry_price, exit_price, entry_fee, exit_fee, volume
        FROM trades
        WHERE exit_reason = 'SETTLED_LOSS'
          AND closed_at >= datetime('now', '-24 hours')
          AND run_id = ? AND paper = 1
    """, (current_run_id,)).fetchall()

    open_count = conn.execute("""
        SELECT COUNT(*) FROM trades
        WHERE exit_price IS NULL AND run_id = ?
    """, (current_run_id,)).fetchone()[0]

    total_pnl = conn.execute("""
        SELECT COALESCE(SUM(
            (exit_price - entry_price) * volume
            - COALESCE(entry_fee, 0) - COALESCE(exit_fee, 0)
        ), 0.0)
        FROM trades
        WHERE exit_price IS NOT NULL
          AND closed_at >= datetime('now', '-24 hours')
          AND run_id = ? AND paper = 1
    """, (current_run_id,)).fetchone()[0] or 0.0

    conn.close()

    settled_wins = len(recent_wins)
    settled_losses = len(recent_losses)
    pnl_str = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"

    send_telegram(
        f"📅 Daily Summary | {datetime.now(tz=UTC).strftime('%b %d')}\n"
        f"✅ Settled wins: {settled_wins}\n"
        f"❌ Settled losses: {settled_losses}\n"
        f"💼 Open positions: {open_count}\n"
        f"💰 24h PnL: {pnl_str}"
    )


def send_telegram(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=5,
        )
    except Exception as e:
        log.warning(f"Telegram error: {e}")


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


def _format_position_age(created_at: str | None) -> str:
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


def _contracts_from_volume(volume) -> float | None:
    if volume is None or volume == 0:
        return None
    try:
        count = int(float(volume))
    except (TypeError, ValueError):
        return None
    return count if count > 0 else None


def _fee_contract_count(volume, entry_price: float) -> int:
    contracts = _contracts_from_volume(volume)
    if contracts is not None:
        return int(contracts)
    if entry_price <= 0:
        return 0
    return int((TRADE_AMOUNT_CENTS / 100) / entry_price)


def _trade_pnl(
    entry: float,
    exit_price: float,
    entry_fee: float = 0.0,
    exit_fee: float = 0.0,
    contracts: float | None = None,
) -> float:
    if entry <= 0:
        return 0.0
    shares = contracts if contracts is not None else (TRADE_AMOUNT_CENTS / 100) / entry
    return shares * (exit_price - entry) - entry_fee - exit_fee


def _avg_pnl(rows: list[tuple]) -> float:
    if not rows:
        return 0.0
    total = 0.0
    for r in rows:
        volume = r[4] if len(r) > 4 else None
        contracts = int(float(volume)) if volume else None
        total += _trade_pnl(
            r[0], r[1],
            entry_fee=(r[2] or 0.0) if len(r) > 2 else 0.0,
            exit_fee=(r[3] or 0.0) if len(r) > 3 else 0.0,
            contracts=contracts,
        )
    return total / len(rows)


def build_telegram_dashboard() -> str:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    open_rows = conn.execute("""
        SELECT series_ticker, market_ticker, bracket_label, entry_price, volume, created_at
        FROM trades
        WHERE exit_price IS NULL AND run_id = ?
        ORDER BY entry_price ASC
    """, (current_run_id,)).fetchall()

    settled_wins = conn.execute("""
        SELECT entry_price, exit_price, entry_fee, exit_fee, volume
        FROM trades
        WHERE exit_reason = 'SETTLED_WIN' AND run_id = ? AND paper = 1
    """, (current_run_id,)).fetchall()

    settled_losses = conn.execute("""
        SELECT entry_price, exit_price, entry_fee, exit_fee, volume
        FROM trades
        WHERE exit_reason = 'SETTLED_LOSS' AND run_id = ? AND paper = 1
    """, (current_run_id,)).fetchall()

    total_pnl = conn.execute("""
        SELECT COALESCE(SUM(
            (exit_price - entry_price) * volume
            - COALESCE(entry_fee, 0) - COALESCE(exit_fee, 0)
        ), 0.0)
        FROM trades
        WHERE exit_price IS NOT NULL AND run_id = ? AND paper = 1
    """, (current_run_id,)).fetchone()[0] or 0.0

    conn.close()

    settled_wins_count = len(settled_wins)
    settled_losses_count = len(settled_losses)
    win_pnls = [
        _trade_pnl(
            r["entry_price"], r["exit_price"],
            entry_fee=(r["entry_fee"] or 0.0),
            exit_fee=(r["exit_fee"] or 0.0),
            contracts=int(float(r["volume"])) if r["volume"] else None,
        )
        for r in settled_wins
    ]
    avg_win_pnl = (sum(win_pnls) / len(win_pnls)) if win_pnls else 0.0

    now = datetime.now(tz=EASTERN)
    total_pnl_str = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"
    total_closed = settled_wins_count + settled_losses_count
    win_rate = (settled_wins_count / total_closed * 100) if total_closed > 0 else 0.0

    lines = [
        f"📊 *Hold Bot* | Run #{current_run_id} | {now.strftime('%Y-%m-%d')} {now.strftime('%H:%M')} ET",
        "",
        f"💼 Open: {len(open_rows)}",
        f"✅ Settled wins: {settled_wins_count} avg +${avg_win_pnl:.2f}",
        f"❌ Settled losses: {settled_losses_count}",
        f"📊 Win rate: {win_rate:.1f}%",
        f"💰 Total PnL: {total_pnl_str}",
        "",
        "*Recent open positions:*",
    ]

    if open_rows:
        for r in open_rows[:5]:
            qty = float(r["volume"] or 0)
            lines.append(
                f"{r['series_ticker']} | {parse_event_date(r['market_ticker'])} | {r['bracket_label']} "
                f"@ {r['entry_price']:.2f} qty {qty:.0f} "
                f"({_format_position_age(r['created_at'])})"
            )
        remaining = len(open_rows) - 5
        if remaining > 0:
            lines.append(f"... and {remaining} more")
    else:
        lines.append("(none)")

    return "\n".join(lines)


def build_balance_report() -> str:
    cash_balance = paper_balance if PAPER_TRADING else fetch_wallet_balance()
    trade_dollars = TRADE_AMOUNT_CENTS / 100

    conn = sqlite3.connect(DB_PATH)
    open_rows = conn.execute("""
        SELECT entry_price, volume
        FROM trades
        WHERE exit_price IS NULL AND run_id = ?
    """, (current_run_id,)).fetchall()
    conn.close()

    open_count = len(open_rows)
    cost_basis = 0.0
    potential_profit = 0.0
    for entry_price, volume in open_rows:
        if not entry_price or entry_price <= 0:
            continue
        actual_contracts = float(volume) if volume else (trade_dollars / entry_price)
        cost_basis += actual_contracts * entry_price
        potential_profit += (1.00 - entry_price) * actual_contracts

    if cash_balance is None:
        remaining_trades = 0
        cash_str = "N/A"
    else:
        remaining_trades = max(
            0, int((cash_balance - MIN_WALLET_BALANCE) / trade_dollars)
        )
        cash_str = f"${cash_balance:.2f}"

    label = "Paper Balance" if PAPER_TRADING else "Cash Balance"
    return (
        f"💵 {label}: {cash_str}\n"
        f"📦 Open positions: {open_count} (cost basis: ${cost_basis:.2f})\n"
        f"✅ If all settle YES: +${potential_profit:.2f}\n"
        f"🔫 Remaining capacity: {remaining_trades} more trades before ${MIN_WALLET_BALANCE:.0f} floor"
    )


def handle_telegram_commands() -> None:
    global telegram_offset, current_run_id, open_positions, trading_paused
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
            params={"offset": telegram_offset, "timeout": 0},
            timeout=5,
        )
        r.raise_for_status()
        for update in r.json().get("result", []):
            telegram_offset = update["update_id"] + 1
            message = update.get("message") or {}
            chat_id = str(message.get("chat", {}).get("id", ""))
            if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
                continue
            text = (message.get("text") or "").strip()
            if text.startswith("/reset_paper"):
                current_run_id = start_new_run(note="manual reset")
                open_positions = {}
                send_telegram(
                    f"🔄 Paper run reset. Now on run #{current_run_id} — "
                    f"all previous trades preserved in DB."
                )
            elif text.startswith("/dashboard"):
                send_telegram(build_telegram_dashboard())
            elif text.startswith("/balance"):
                send_telegram(build_balance_report())
            elif text.startswith("/pause"):
                trading_paused = True
                set_state("trading_paused", "true")
                send_telegram(
                    "⏸️ Trading PAUSED\n"
                    "New entries and top-ups are suspended.\n"
                    "Monitoring and settlements continue.\n"
                    "Send /resume to resume trading."
                )
                log.info("Trading paused via Telegram command")
            elif text.startswith("/resume"):
                trading_paused = False
                set_state("trading_paused", "false")
                send_telegram(
                    "▶️ Trading RESUMED\n"
                    "New entries and top-ups are active again."
                )
                log.info("Trading resumed via Telegram command")
    except Exception as e:
        log.warning(f"Telegram command polling error: {e}")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
# ENSEMBLE — not used in market-based strategy
# def _scheduled_ensemble_update():
#     global watched
#     watched = run_ensemble_update()


def _scheduled_build_watchlist():
    global watchlist_is_valid
    if not is_open_snapshot_window():
        return
    if not try_build_watchlist():
        schedule.clear("watchlist_retry")
        schedule.every(5).minutes.do(_retry_watchlist_build).tag("watchlist_retry")
    watchlist_is_valid = True



def _retry_watchlist_build():
    schedule.clear("watchlist_retry")
    if not try_build_watchlist():
        schedule.every(5).minutes.do(_retry_watchlist_build).tag("watchlist_retry")
    else:
        log.info("Watchlist fully built")


def _scheduled_watchlist_monitor():
    reset_cycle_spent()
    run_watchlist_monitor()


# ENSEMBLE — not used in market-based strategy
# def _scheduled_refresh_markets():
#     refresh_markets()


def main():
    global watchlist, watchlist_is_valid, open_positions, current_run_id, trading_paused

    init_db()
    trading_paused = get_state("trading_paused", "false") == "true"
    if trading_paused:
        log.info("Resuming in PAUSED state from previous session")
    current_run_id = get_or_create_run()
    open_positions = load_open_positions()
    log.info(f"Loaded {len(open_positions)} open positions from DB")
    if not PAPER_TRADING:
        reconcile_positions()
    log.info(f"Kalshi Market Bot | PAPER={PAPER_TRADING}")
    log.info(
        f"Config: ENTRY_CAP={ENTRY_CAP} ENTRY_FLOOR={ENTRY_FLOOR} "
        f"TRADE_AMOUNT_CENTS={TRADE_AMOUNT_CENTS} "
        f"PAPER_STARTING_BALANCE={PAPER_STARTING_BALANCE} "
        f"WATCHLIST_SIZE={WATCHLIST_SIZE} TRADING_PAUSED={TRADING_PAUSED}"
    )
    disabled = sorted(s for s, c in SERIES_CONFIG.items() if not c.get("enabled", True))
    if ENABLED_SERIES:
        log.info(f"ENABLED_SERIES override: {ENABLED_SERIES}")
    else:
        log.info("ENABLED_SERIES: all default series active")
    log.info(f"Disabled series: {', '.join(disabled) if disabled else '(none)'}")
    send_telegram(f"🤖 Kalshi Market Bot started | PAPER={PAPER_TRADING}")
    if trading_paused:
        send_telegram(
            "⏸️ Bot started in PAUSED mode\n"
            "New entries and top-ups are suspended.\n"
            "Send /resume to begin trading."
        )
        log.info("Bot started with TRADING_PAUSED=true")
    handle_telegram_commands()

    watchlist = load_watchlist_from_db()

    tomorrow = _tomorrow_utc_date()
    conn = sqlite3.connect(DB_PATH)
    tomorrow_count = conn.execute(
        "SELECT COUNT(*) FROM watchlist WHERE event_date = ?",
        (tomorrow,),
    ).fetchone()[0]
    conn.close()

    now = datetime.now(tz=UTC)
    past_snapshot_time = (now.hour > 14) or (now.hour == 14 and now.minute >= 5)
    should_build = past_snapshot_time and tomorrow_count == 0

    if should_build:
        log.info("Startup: past 14:05 UTC and no tomorrow watchlist found — building now")
        if not try_build_watchlist():
            schedule.clear("watchlist_retry")
            schedule.every(5).minutes.do(_retry_watchlist_build).tag("watchlist_retry")
        watchlist = load_watchlist_from_db()
        watchlist_is_valid = True
    elif watchlist:
        watchlist_is_valid = True
        dates = sorted(set(info.get("event_date") for info in watchlist.values()))
        log.info(
            f"Loaded watchlist from DB: {len(watchlist)} events across dates {dates}"
        )
    else:
        watchlist_is_valid = False
        log.info("No watchlist found and before snapshot window — waiting for 14:05 UTC")
        send_telegram(
            "⏳ Market Bot started — no watchlist found; waiting for 14:05 UTC build"
        )

    if "--run-now" in sys.argv:
        run_watchlist_monitor()
        check_fills()
        return

    run_watchlist_monitor()
    check_fills()

    # ENSEMBLE — not used in market-based strategy
    # for hour in ("03:30", "09:30", "15:30", "21:30"):
    #     schedule.every().day.at(hour).do(_scheduled_ensemble_update)
    # schedule.every().day.at("14:05").do(_scheduled_refresh_markets)
    schedule.every().day.at("14:05").do(_scheduled_build_watchlist)
    schedule.every(SCAN_INTERVAL_MINUTES).minutes.do(_scheduled_watchlist_monitor)
    schedule.every(SCAN_INTERVAL_MINUTES).minutes.do(check_fills)
    schedule.every(SCAN_INTERVAL_MINUTES).minutes.do(run_price_history_capture)
    schedule.every(10).minutes.do(check_health)
    schedule.every().day.at("12:00").do(send_daily_summary)
    schedule.every(5).seconds.do(handle_telegram_commands)
    log.info(
        f"Scheduled: watchlist build daily 14:05 UTC; monitor + fills + price history "
        f"every {SCAN_INTERVAL_MINUTES} min; "
        "health check every 10 min; daily summary 12:00 UTC; Telegram every 5s"
    )

    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()
