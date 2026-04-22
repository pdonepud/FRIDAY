"""
stocks.py — FRIDAY's market sense.

Pulls current price + daily change for every ticker in config.STOCK_TICKERS
from Alpha Vantage. Aggressively caches to stay under the free tier's
25-calls-per-day cap.

Test directly:
    python modules/stocks.py
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import requests

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import ALPHAVANTAGE_KEY, STOCK_TICKERS


_ENDPOINT = "https://www.alphavantage.co/query"
_TIMEOUT_SECONDS = 15
_CACHE_TTL_SECONDS = 15 * 60  # 15 minutes per ticker
_API_DELAY_SECONDS = 1.2  # Alpha Vantage free tier: max 1 request per second

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CACHE_FILE = _PROJECT_ROOT / "data" / "stocks_cache.json"

_FLAT_THRESHOLD_PCT = 0.2  # |change_pct| < 0.2% is considered "flat"

_COMPANY_NAMES: Dict[str, str] = {
    "AAPL":  "Apple",
    "TSLA":  "Tesla",
    "NVDA":  "Nvidia",
    "MSFT":  "Microsoft",
    "GOOGL": "Google",
    "GOOG":  "Google",
    "AMZN":  "Amazon",
    "META":  "Meta",
    "AMD":   "AMD",
    "AMAT":  "Applied Materials",
}

# ANSI colors
_GREEN = "\033[92m"
_RED   = "\033[91m"
_GRAY  = "\033[90m"
_RESET = "\033[0m"


def _load_cache() -> Dict[str, Dict]:
    if not _CACHE_FILE.exists():
        return {}
    try:
        with _CACHE_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache: Dict[str, Dict]) -> None:
    _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _CACHE_FILE.open("w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def _fetch_from_api(ticker: str) -> Optional[Dict]:
    """
    Hit Alpha Vantage. Return raw quote dict on success, None on rate-limit
    or empty response. Raises on network errors.
    """
    params = {"function": "GLOBAL_QUOTE", "symbol": ticker, "apikey": ALPHAVANTAGE_KEY}
    response = requests.get(_ENDPOINT, params=params, timeout=_TIMEOUT_SECONDS)
    response.raise_for_status()
    data = response.json()

    if "Information" in data or "Note" in data:
        msg = data.get("Information") or data.get("Note")
        print(f"[stocks] Alpha Vantage rate limit / notice for {ticker}: {msg}")
        return None

    quote = data.get("Global Quote")
    if not quote or "05. price" not in quote:
        print(f"[stocks] Empty quote for {ticker}: {data}")
        return None

    pct_str = quote.get("10. change percent", "0%").rstrip("%")
    return {
        "price":      float(quote["05. price"]),
        "change":     float(quote["09. change"]),
        "change_pct": float(pct_str),
        "fetched_at": time.time(),
    }


def get_quote(ticker: str) -> Optional[Dict]:
    """
    Get a single quote, using cache when fresh.

    Returns:
        {"ticker": "AAPL", "price": 234.50, "change": 1.20,
         "change_pct": 0.51, "stale": False}
        or None if no data is available (cache miss + API failure).
    """
    cache = _load_cache()
    cached = cache.get(ticker)
    now = time.time()

    if cached and (now - cached.get("fetched_at", 0)) < _CACHE_TTL_SECONDS:
        print(f"[stocks] Cache hit: {ticker}")
        return {
            "ticker":     ticker,
            "price":      cached["price"],
            "change":     cached["change"],
            "change_pct": cached["change_pct"],
            "stale":      False,
        }

    print(f"[stocks] Fetching: {ticker}")
    try:
        fresh = _fetch_from_api(ticker)
    except (requests.Timeout, requests.RequestException) as e:
        print(f"[stocks] API error for {ticker}: {e}")
        fresh = None

    if fresh:
        cache[ticker] = fresh
        _save_cache(cache)
        return {
            "ticker":     ticker,
            "price":      fresh["price"],
            "change":     fresh["change"],
            "change_pct": fresh["change_pct"],
            "stale":      False,
        }

    # API failed — fall back to cached value if we have one, marked stale.
    if cached:
        return {
            "ticker":     ticker,
            "price":      cached["price"],
            "change":     cached["change"],
            "change_pct": cached["change_pct"],
            "stale":      True,
        }

    print(f"[stocks] No data available for {ticker} (cache miss + API failure).")
    return None


def get_watchlist() -> List[Dict]:
    """
    Return quotes for every ticker in STOCK_TICKERS, skipping failures.

    Throttles between API calls (cache hits don't sleep) so the free tier's
    1-request-per-second limit isn't tripped.
    """
    quotes: List[Dict] = []
    last_api_call = 0.0
    cache = _load_cache()
    now = time.time()

    for ticker in STOCK_TICKERS:
        cached = cache.get(ticker)
        will_hit_api = not cached or (now - cached.get("fetched_at", 0)) >= _CACHE_TTL_SECONDS

        if will_hit_api:
            elapsed = time.time() - last_api_call
            if last_api_call and elapsed < _API_DELAY_SECONDS:
                time.sleep(_API_DELAY_SECONDS - elapsed)
            last_api_call = time.time()

        q = get_quote(ticker)
        if q is not None:
            quotes.append(q)
    return quotes


def _vibe(quotes: List[Dict]) -> str:
    """Overall watchlist vibe: 'mostly up', 'mostly down', 'mixed', or 'flat'."""
    ups = sum(1 for q in quotes if q["change_pct"] > _FLAT_THRESHOLD_PCT)
    downs = sum(1 for q in quotes if q["change_pct"] < -_FLAT_THRESHOLD_PCT)
    flats = len(quotes) - ups - downs
    total = len(quotes)
    if total == 0:
        return "flat"
    if flats == total:
        return "flat"
    if ups >= total * 0.7:
        return "mostly up"
    if downs >= total * 0.7:
        return "mostly down"
    return "mixed"


def _phrase_one(q: Dict) -> str:
    """
    Spoken-friendly phrase for a single quote.

    Single-word mapped names get a possessive ("Apple's up 1.2 percent").
    Multi-word names and unmapped tickers use "is" to avoid awkward "'s"
    ("Applied Materials is up 2.3 percent", "PLTR is up 4 percent").
    """
    ticker = q["ticker"]
    mapped = _COMPANY_NAMES.get(ticker)
    if mapped and " " not in mapped:
        name = mapped
        verb_flat, verb_move = "'s flat", "'s"
    else:
        name = mapped if mapped else ticker
        verb_flat, verb_move = " is flat", " is"

    pct = q["change_pct"]
    if abs(pct) < _FLAT_THRESHOLD_PCT:
        return f"{name}{verb_flat}"
    direction = "up" if pct > 0 else "down"
    return f"{name}{verb_move} {direction} {abs(pct):.1f} percent to {q['price']:.2f}"


def describe_watchlist(quotes: Optional[List[Dict]] = None) -> str:
    """
    One spoken-friendly sentence summarizing the whole watchlist.

    Pass an existing quotes list to avoid a second round of API/cache lookups.
    """
    if quotes is None:
        quotes = get_watchlist()
    if not quotes:
        return "Watchlist data unavailable right now."

    vibe = _vibe(quotes)
    parts = [_phrase_one(q) for q in quotes]
    return f"Your watchlist is {vibe} today. " + ". ".join(parts) + "."


def _color_for(change: float) -> str:
    if change > _FLAT_THRESHOLD_PCT:
        return _GREEN
    if change < -_FLAT_THRESHOLD_PCT:
        return _RED
    return _GRAY


if __name__ == "__main__":
    quotes = get_watchlist()

    if not quotes:
        print("No quotes available.")
        sys.exit(0)

    any_stale = False
    for q in quotes:
        color = _color_for(q["change_pct"])
        marker = "*" if q["stale"] else " "
        any_stale = any_stale or q["stale"]
        sign = "+" if q["change"] >= 0 else ""
        print(
            f"{q['ticker']:<6}{marker} "
            f"${q['price']:>8.2f}    "
            f"{color}{sign}{q['change']:>6.2f}  "
            f"({sign}{q['change_pct']:>5.2f}%){_RESET}"
        )

    print()
    print(describe_watchlist(quotes))

    if any_stale:
        print()
        print("* = cached value (API unavailable)")
