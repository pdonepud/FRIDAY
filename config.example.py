"""
config.example.py — Template for FRIDAY's configuration.

HOW TO USE:
    1. Copy this file to `config.py` (which is gitignored).
         Windows PowerShell:  Copy-Item config.example.py config.py
         Mac/Linux:           cp config.example.py config.py
    2. Fill in your API keys in the new config.py.
    3. Never commit config.py — it's in .gitignore for a reason.

Get your free API keys:
    Gemini:        https://aistudio.google.com
    NewsAPI:       https://newsapi.org
    Porcupine:     https://console.picovoice.ai  (optional, for voice wake)

Stocks use yfinance (no API key required) as of Phase 5.8.
"""

# ========== API KEYS ==========
GEMINI_API_KEY   = ""   # aistudio.google.com
NEWSAPI_KEY      = ""   # newsapi.org
# ALPHAVANTAGE_KEY removed in Phase 5.8 — switched to yfinance (no key required)
PORCUPINE_KEY    = ""   # picovoice.ai (optional)

# Google Calendar uses credentials.json (OAuth), not an API key.
# We'll set that up in Phase 4.

# ========== PERSONAL ==========
USER_NAME = "Preetam"
WAKE_TIME = "07:00"

# Salinas, CA (change to your location)
LATITUDE  = 36.6777
LONGITUDE = -121.6555

# ========== WATCHLIST ==========
STOCK_TICKERS = ["AAPL", "TSLA", "NVDA", "MSFT"]

# ========== NEWS TOPICS ==========
# Each entry: a labeled news topic with optional source whitelisting.
#   - "label":     short name for logging/cache key/debugging
#   - "category":  used by the briefing to balance coverage. Options:
#                  "tech", "markets", "politics", "world"
#   - "q":         NewsAPI query string. Quote phrases for exact match.
#                  Use AND/OR for boolean logic.
#   - "domains":   comma-separated NewsAPI domains to restrict sources.
#                  Empty string = no restriction. Common mainstream domains:
#                  reuters.com, apnews.com, washingtonpost.com,
#                  bbc.com, bloomberg.com, cnbc.com, axios.com,
#                  theverge.com, techcrunch.com, arstechnica.com,
#                  nytimes.com
NEWS_TOPICS = [
    {
        "label": "tech",
        "category": "tech",
        "q": "technology",
        "domains": "techcrunch.com,arstechnica.com,theverge.com,reuters.com",
    },
    {
        "label": "ai",
        "category": "tech",
        "q": '"artificial intelligence"',
        "domains": "",  # broader sourcing — AI coverage varies a lot
    },
    {
        "label": "markets",
        "category": "markets",
        "q": "stock market",
        "domains": "bloomberg.com,reuters.com,cnbc.com",
    },
    {
        "label": "us_politics",
        "category": "politics",
        "q": "Congress OR Senate OR \"White House\" OR \"Supreme Court\"",
        "domains": "reuters.com,apnews.com,washingtonpost.com,axios.com",
    },
    {
        "label": "trump",
        "category": "politics",
        "q": '"Trump" AND (executive OR administration OR signs OR announces OR order)',
        "domains": "reuters.com,apnews.com,washingtonpost.com",
    },
    {
        "label": "world",
        "category": "world",
        "q": '"foreign policy" OR Russia OR China OR Israel OR Ukraine',
        "domains": "reuters.com,bbc.com,apnews.com",
    },
]

# ========== VOICE ==========
# edge-tts voice. Full list: `edge-tts --list-voices`
# Good options:
#   en-US-JennyNeural   (warm female, default)
#   en-US-GuyNeural     (male US)
#   en-US-AriaNeural    (female US, newscaster-y)
#   en-GB-SoniaNeural   (female UK)
VOICE = "en-US-JennyNeural"

# ========== BEHAVIOR ==========
NUDGE_INTERVAL_MIN = 45
FOCUS_BLOCKLIST    = ["youtube.com", "reddit.com", "twitter.com", "x.com"]

# ========== HOTKEYS ==========
HOTKEY_WAKE      = "ctrl+alt+f"
HOTKEY_CLIPBOARD = "ctrl+alt+c"
HOTKEY_NEXT      = "ctrl+alt+n"
HOTKEY_FOCUS     = "ctrl+alt+m"
HOTKEY_QUIT      = "ctrl+alt+q"
