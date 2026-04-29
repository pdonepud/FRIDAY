"""
news.py — FRIDAY's news desk.

Pulls top headlines from NewsAPI for every topic in config.NEWS_TOPICS.
Caches per-topic for 30 minutes to respect the free tier's 100/day cap.

Test directly:
    python modules/news.py
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import requests

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import NEWSAPI_KEY, NEWS_TOPICS


_ENDPOINT = "https://newsapi.org/v2/everything"
_TIMEOUT_SECONDS = 15
_CACHE_TTL_SECONDS = 30 * 60  # 30 minutes per topic
_PAGE_SIZE = 5

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CACHE_FILE = _PROJECT_ROOT / "data" / "news_cache.json"

_STOPWORDS = {
    "the", "a", "an", "is", "in", "to", "for", "on", "of", "at", "by", "with",
}
_DEDUP_THRESHOLD = 0.70


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


def _is_fresh(cached: Optional[Dict], now: float) -> bool:
    return bool(cached) and (now - cached.get("fetched_at", 0)) < _CACHE_TTL_SECONDS


def _fetch_from_api(topic: str) -> Optional[List[Dict]]:
    """
    Hit NewsAPI /v2/everything for one topic.

    Returns a list of cleaned article dicts on success, None on API-level
    failure (status=error). Raises on network errors.
    """
    params = {
        "q":        topic,
        "language": "en",
        "sortBy":   "publishedAt",
        "searchIn": "title,description",
        "pageSize": _PAGE_SIZE,
        "apiKey":   NEWSAPI_KEY,
    }
    response = requests.get(_ENDPOINT, params=params, timeout=_TIMEOUT_SECONDS)
    response.raise_for_status()
    data = response.json()

    if data.get("status") == "error":
        print(f"[news] NewsAPI error for {topic}: {data.get('message')}")
        return None

    articles = data.get("articles", [])
    cleaned: List[Dict] = []
    for a in articles:
        cleaned.append({
            "title":       a.get("title") or "",
            "source":      (a.get("source") or {}).get("name") or "",
            "url":         a.get("url") or "",
            "description": a.get("description") or "",
        })
    return cleaned


def get_headlines(topic: str) -> List[Dict]:
    """
    Return up to 5 headlines for one topic, cache-aware.

    On cache hit (within TTL): returns cached articles with stale=False.
    On API success: refreshes cache, returns articles with stale=False.
    On API failure: falls back to stale cache (stale=True), or [] if no cache.
    """
    cache = _load_cache()
    cached = cache.get(topic)
    now = time.time()

    if _is_fresh(cached, now):
        print(f"[news] Cache hit: {topic}")
        return [{**a, "stale": False} for a in cached["articles"]]

    print(f"[news] Fetching: {topic}")
    try:
        fresh = _fetch_from_api(topic)
    except (requests.Timeout, requests.RequestException) as e:
        print(f"[news] API error for {topic}: {e}")
        fresh = None

    if fresh is not None:
        cache[topic] = {"fetched_at": now, "articles": fresh}
        _save_cache(cache)
        return [{**a, "stale": False} for a in fresh]

    if cached:
        return [{**a, "stale": True} for a in cached["articles"]]

    return []


def _normalize_title(title: str) -> set:
    """Lowercase, strip punctuation, drop stopwords. Returns significant token set."""
    cleaned = re.sub(r"[^\w\s]", " ", title.lower())
    return {tok for tok in cleaned.split() if tok and tok not in _STOPWORDS}


def _is_duplicate(tokens_a: set, tokens_b: set) -> bool:
    if not tokens_a or not tokens_b:
        return False
    smaller = min(len(tokens_a), len(tokens_b))
    overlap = len(tokens_a & tokens_b)
    return overlap / smaller >= _DEDUP_THRESHOLD


def get_all_headlines(max_total: int = 8) -> List[Dict]:
    """
    Pull headlines from every topic in NEWS_TOPICS, interleave round-robin so
    one topic doesn't dominate, dedupe by title similarity, and trim to
    max_total.

    Each returned article has a "topic" key noting which feed it came from.
    """
    by_topic: Dict[str, List[Dict]] = {}
    for topic in NEWS_TOPICS:
        articles = get_headlines(topic)
        for a in articles:
            a["topic"] = topic
        by_topic[topic] = articles

    # Round-robin: t1[0], t2[0], t3[0], t1[1], t2[1], ...
    interleaved: List[Dict] = []
    longest = max((len(v) for v in by_topic.values()), default=0)
    for i in range(longest):
        for topic in NEWS_TOPICS:
            if i < len(by_topic[topic]):
                interleaved.append(by_topic[topic][i])

    deduped: List[Dict] = []
    seen_tokens: List[set] = []
    for article in interleaved:
        tokens = _normalize_title(article["title"])
        if any(_is_duplicate(tokens, prev) for prev in seen_tokens):
            continue
        deduped.append(article)
        seen_tokens.append(tokens)
        if len(deduped) >= max_total:
            break

    return deduped


def _clean_for_speech(title: str) -> str:
    """Strip all-caps prefixes (BREAKING:, etc.) and trailing site names."""
    title = re.sub(r"^[A-Z]{2,}:\s*", "", title)
    for sep in (" - ", " | "):
        if sep in title:
            head, tail = title.rsplit(sep, 1)
            if len(tail) < 30:
                title = head
                break
    return title.strip()


def describe_news() -> str:
    """Spoken-friendly summary of the top 3 deduped headlines."""
    articles = get_all_headlines(max_total=3)
    if not articles:
        return "No news to report right now."

    numbers = ("One", "Two", "Three")
    parts: List[str] = []
    for i, article in enumerate(articles[:3]):
        parts.append(f"{numbers[i]}, {_clean_for_speech(article['title'])}")
    return "Top stories right now. " + ". ".join(parts) + "."


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows console renders curly quotes etc.

    cache_at_start = _load_cache()
    start_time = time.time()
    fresh_count = sum(
        1 for t in NEWS_TOPICS if _is_fresh(cache_at_start.get(t), start_time)
    )

    print("=== Top Headlines ===")
    print(f"[cache: {fresh_count}/{len(NEWS_TOPICS)} topics fresh]")
    print()

    any_stale = False
    for topic in NEWS_TOPICS:
        articles = get_headlines(topic)
        print(f"[{topic}]")
        if not articles:
            print("  (no articles)")
        for i, a in enumerate(articles, 1):
            stale_marker = " *" if a.get("stale") else ""
            any_stale = any_stale or a.get("stale", False)
            source = f" ({a['source']})" if a["source"] else ""
            print(f"  {i}. {a['title']}{source}{stale_marker}")
        print()

    print("=== Spoken Summary ===")
    print(describe_news())

    if any_stale:
        print()
        print("* = cached value (API unavailable)")
