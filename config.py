"""
FRIDAY Config
-------------
Fill in your API keys and personal settings.
Leave keys as empty strings for now if you don't have them yet —
Phase 1 (greeting test) doesn't need any of them.
"""

# ========== API KEYS ==========
GEMINI_API_KEY   = ""   # aistudio.google.com
NEWSAPI_KEY      = ""   # newsapi.org
ALPHAVANTAGE_KEY = ""   # alphavantage.co
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
NEWS_TOPICS   = ["technology", "AI", "markets"]

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