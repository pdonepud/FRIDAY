"""
dashboard.py — FRIDAY's tkinter dashboard.

Dark-mode at-a-glance window: header (greeting / time / weather), today's
events, watchlist, top headlines, and action buttons.

Live-updating clock in the header ticks every second; the Refresh button
re-fetches every data source and rebuilds the panels.

Run standalone:
    python modules/dashboard.py
"""

import os
import sys
import tkinter as tk
from datetime import datetime
from tkinter import ttk
from typing import List, Dict

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import USER_NAME
from modules.weather import get_weather
from modules.calendar_api import get_today
from modules.stocks import get_watchlist
from modules.news import get_balanced_headlines


# ========== COLOR PALETTE (GitHub dark) ==========
BG          = "#0d1117"   # window background
PANEL       = "#161b22"   # panel/card background
BORDER      = "#21262d"   # subtle borders
TEXT        = "#e6edf3"   # primary text
TEXT_DIM    = "#7d8590"   # secondary/label text
ACCENT      = "#58a6ff"   # blue accent (links, highlights)
GREEN       = "#3fb950"   # positive stock movement
RED         = "#f85149"   # negative stock movement
YELLOW      = "#d29922"   # warning/category tags
PURPLE      = "#bc8cff"   # category accent

_CATEGORY_COLORS = {
    "politics": PURPLE,
    "world":    YELLOW,
    "markets":  GREEN,
    "tech":     ACCENT,
}

_FLAT_THRESHOLD_PCT = 0.2  # |change_pct| below this counts as flat


# ========== PLACEHOLDER DATA ==========
# NOTE: Placeholders retained for testing offline / when APIs are down.
# The dashboard normally uses live data via _get_*_data() helpers.
PLACEHOLDER_GREETING = "Good evening, Preetam"
PLACEHOLDER_TIME = "7:42 PM"
PLACEHOLDER_WEATHER = "58°F overcast"

PLACEHOLDER_EVENTS = [
    {"time": "1:30 PM", "title": "CSE 101", "all_day": False},
    {"time": "", "title": "Buddha Purnima", "all_day": True},
]

PLACEHOLDER_STOCKS = [
    {"ticker": "AMD",  "price": 303.46, "change_pct": 6.67},
    {"ticker": "TSLA", "price": 387.51, "change_pct": 0.28},
    {"ticker": "NVDA", "price": 202.50, "change_pct": 1.31},
    {"ticker": "AMAT", "price": 403.48, "change_pct": 2.32},
]

PLACEHOLDER_HEADLINES = [
    {"category": "politics", "title": "Supreme Court rules on districts", "source": "AP"},
    {"category": "world",    "title": "Ukraine ventilator scandal", "source": "BBC"},
    {"category": "tech",     "title": "Apple announces M5 chip", "source": "The Verge"},
]


# ========== DATA GATHERING ==========
# Each helper wraps a data source in try/except so one outage can't take
# down the whole window. Returns the shape the matching _build_*_panel
# expects, or a safe empty fallback.

def _fmt_clock(dt: datetime) -> str:
    """12-hour clock with no leading zero, e.g. '7:42 PM' (cross-platform)."""
    if sys.platform == "win32":
        return dt.strftime("%#I:%M %p")
    return dt.strftime("%-I:%M %p")


def _get_header_data() -> Dict:
    """Returns {greeting, time_str, weather}. Falls back to safe defaults on failure."""
    now = datetime.now()
    hour = now.hour
    if hour < 12:
        greeting = f"Good morning, {USER_NAME}"
    elif hour < 17:
        greeting = f"Good afternoon, {USER_NAME}"
    elif hour < 22:
        greeting = f"Good evening, {USER_NAME}"
    else:
        greeting = f"It's late, {USER_NAME}"

    time_str = _fmt_clock(now)

    try:
        w = get_weather()
        weather = f"{round(w['temp_f'])}°F {w['conditions']}"
    except Exception as e:
        print(f"[dashboard] Weather failed: {e}")
        weather = "weather unavailable"

    return {"greeting": greeting, "time_str": time_str, "weather": weather}


def _get_events_data() -> List[Dict]:
    """Returns list of {time, title, all_day} dicts for today."""
    try:
        events = get_today()
    except Exception as e:
        print(f"[dashboard] Events failed: {e}")
        return []

    result: List[Dict] = []
    for e in events:
        if e["all_day"]:
            result.append({"time": "", "title": e["title"], "all_day": True})
        else:
            result.append({
                "time": _fmt_clock(e["start"]),
                "title": e["title"],
                "all_day": False,
            })
    return result


def _get_stocks_data() -> List[Dict]:
    """Returns list of {ticker, price, change_pct} dicts."""
    try:
        watchlist = get_watchlist()
        return [
            {"ticker": s["ticker"], "price": s["price"], "change_pct": s["change_pct"]}
            for s in watchlist
        ]
    except Exception as e:
        print(f"[dashboard] Stocks failed: {e}")
        return []


def _get_headlines_data() -> List[Dict]:
    """Returns list of {category, title, source} dicts. Top 5 from balanced selection."""
    try:
        articles = get_balanced_headlines({"politics": 2, "world": 1, "markets": 1, "tech": 1})
        return [
            {"category": a["category"], "title": a["title"], "source": a["source"]}
            for a in articles[:5]
        ]
    except Exception as e:
        print(f"[dashboard] News failed: {e}")
        return []


# ========== HELPERS ==========
def _panel(parent: tk.Misc) -> tk.Frame:
    """Card-style frame with subtle border."""
    return tk.Frame(
        parent,
        bg=PANEL,
        highlightthickness=1,
        highlightbackground=BORDER,
    )


def _section_header(parent: tk.Misc, text: str) -> tk.Label:
    """Uppercase dim label used as a section heading."""
    return tk.Label(
        parent,
        text=text.upper(),
        bg=PANEL,
        fg=TEXT_DIM,
        font=("Segoe UI", 10, "bold"),
        anchor="w",
    )


def _arrow_and_color(change_pct: float):
    """Return (arrow_glyph, color) for a percent change."""
    if change_pct > _FLAT_THRESHOLD_PCT:
        return "▲", GREEN
    if change_pct < -_FLAT_THRESHOLD_PCT:
        return "▼", RED
    return "▬", TEXT_DIM


# ========== PANEL BUILDERS ==========
def _build_header(parent: tk.Misc, greeting: str, time_str: str, weather: str):
    """Build the header frame. Returns (frame, clock_label) so the live-clock
    updater can target the time label directly."""
    frame = _panel(parent)

    inner = tk.Frame(frame, bg=PANEL)
    inner.pack(fill="both", expand=True, padx=16, pady=18)

    tk.Label(
        inner, text=greeting, bg=PANEL, fg=TEXT,
        font=("Segoe UI", 16, "bold"),
    ).pack(side=tk.LEFT)

    tk.Label(
        inner, text="  ·  ", bg=PANEL, fg=TEXT_DIM,
        font=("Segoe UI", 14),
    ).pack(side=tk.LEFT)

    # Fixed width keeps the layout stable when minute/hour digits change
    # (e.g. "9:59 AM" → "10:00 AM" should not jiggle the weather text).
    clock_label = tk.Label(
        inner, text=time_str, bg=PANEL, fg=TEXT,
        font=("Segoe UI", 14), width=8, anchor="w",
    )
    clock_label.pack(side=tk.LEFT)

    tk.Label(
        inner, text="  ·  ", bg=PANEL, fg=TEXT_DIM,
        font=("Segoe UI", 14),
    ).pack(side=tk.LEFT)

    tk.Label(
        inner, text=weather, bg=PANEL, fg=TEXT_DIM,
        font=("Segoe UI", 14),
    ).pack(side=tk.LEFT)

    return frame, clock_label


def _build_today_panel(parent: tk.Misc, events: List[Dict]) -> tk.Frame:
    frame = _panel(parent)

    _section_header(frame, "Today").pack(fill="x", padx=16, pady=(12, 8))

    if not events:
        tk.Label(
            frame, text="Nothing scheduled today.", bg=PANEL, fg=TEXT_DIM,
            font=("Segoe UI", 11), anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 12))
        return frame

    for event in events:
        row = tk.Frame(frame, bg=PANEL)
        row.pack(fill="x", padx=16, pady=4)

        tk.Label(
            row, text="•", bg=PANEL, fg=ACCENT,
            font=("Segoe UI", 11, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 8))

        time_text = "all day" if event.get("all_day") else event.get("time", "")
        if time_text:
            tk.Label(
                row, text=time_text, bg=PANEL, fg=TEXT_DIM,
                font=("Consolas", 10), width=9, anchor="w",
            ).pack(side=tk.LEFT)

        tk.Label(
            row, text=event.get("title", ""), bg=PANEL, fg=TEXT,
            font=("Segoe UI", 11), anchor="w",
        ).pack(side=tk.LEFT)

    # bottom padding
    tk.Frame(frame, bg=PANEL, height=8).pack()

    return frame


def _build_watchlist_panel(parent: tk.Misc, stocks: List[Dict]) -> tk.Frame:
    frame = _panel(parent)

    _section_header(frame, "Watchlist").pack(fill="x", padx=16, pady=(12, 8))

    if not stocks:
        tk.Label(
            frame, text="Watchlist unavailable.", bg=PANEL, fg=TEXT_DIM,
            font=("Segoe UI", 11), anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 12))
        return frame

    for stock in stocks:
        row = tk.Frame(frame, bg=PANEL)
        row.pack(fill="x", padx=16, pady=4)

        tk.Label(
            row, text=stock["ticker"], bg=PANEL, fg=TEXT,
            font=("Segoe UI", 11, "bold"), width=6, anchor="w",
        ).pack(side=tk.LEFT)

        tk.Label(
            row, text=f"${stock['price']:.2f}", bg=PANEL, fg=TEXT,
            font=("Consolas", 11), width=10, anchor="w",
        ).pack(side=tk.LEFT)

        arrow, color = _arrow_and_color(stock["change_pct"])

        tk.Label(
            row, text=arrow, bg=PANEL, fg=color,
            font=("Segoe UI", 11),
        ).pack(side=tk.LEFT, padx=(0, 4))

        tk.Label(
            row, text=f"{stock['change_pct']:+.2f}%", bg=PANEL, fg=color,
            font=("Consolas", 11), anchor="w",
        ).pack(side=tk.LEFT)

    tk.Frame(frame, bg=PANEL, height=8).pack()

    return frame


def _build_news_panel(parent: tk.Misc, headlines: List[Dict]) -> tk.Frame:
    frame = _panel(parent)

    _section_header(frame, "Top Headlines").pack(fill="x", padx=16, pady=(12, 8))

    if not headlines:
        tk.Label(
            frame, text="No headlines.", bg=PANEL, fg=TEXT_DIM,
            font=("Segoe UI", 11), anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 12))
        return frame

    for article in headlines:
        row = tk.Frame(frame, bg=PANEL)
        row.pack(fill="x", padx=16, pady=4)

        category = article.get("category", "")
        chip_color = _CATEGORY_COLORS.get(category, TEXT_DIM)

        tk.Label(
            row, text=f"[{category}]", bg=PANEL, fg=chip_color,
            font=("Consolas", 10, "bold"), width=11, anchor="w",
        ).pack(side=tk.LEFT)

        tk.Label(
            row, text=article.get("title", ""), bg=PANEL, fg=TEXT,
            font=("Segoe UI", 11), anchor="w",
        ).pack(side=tk.LEFT)

        source = article.get("source", "")
        if source:
            tk.Label(
                row, text=f"({source})", bg=PANEL, fg=TEXT_DIM,
                font=("Segoe UI", 9), anchor="w",
            ).pack(side=tk.LEFT, padx=(8, 0))

    tk.Frame(frame, bg=PANEL, height=8).pack()

    return frame


def _build_button_bar(parent: tk.Misc, on_refresh, on_briefing) -> tk.Frame:
    frame = tk.Frame(parent, bg=BG)

    button_kwargs = dict(
        bg=PANEL,
        fg=ACCENT,
        activebackground=BORDER,
        activeforeground=TEXT,
        bd=0,
        relief="flat",
        padx=20,
        pady=10,
        font=("Segoe UI", 10),
        cursor="hand2",
    )

    inner = tk.Frame(frame, bg=BG)
    inner.pack(anchor="center")

    tk.Button(inner, text="Refresh", command=on_refresh, **button_kwargs).pack(
        side=tk.LEFT, padx=8
    )
    tk.Button(inner, text="Run Briefing", command=on_briefing, **button_kwargs).pack(
        side=tk.LEFT, padx=8
    )

    return frame


# ========== LIVE UPDATES ==========
def _tick_clock(state: Dict):
    """Update the clock label every second."""
    if state.get("clock_label") is not None:
        try:
            state["clock_label"].config(text=_fmt_clock(datetime.now()))
        except tk.TclError:
            # Widget was destroyed (window closing) — stop scheduling.
            return
    state["root"].after(1000, lambda: _tick_clock(state))


def _refresh_dashboard(state: Dict):
    """Re-fetch every source and rebuild the data panels (and the header)."""
    print("[dashboard] Refreshing...")

    # Destroy the existing panels before rebuilding.
    for key in ("header", "today_panel", "watchlist_panel", "news_panel"):
        widget = state.get(key)
        if widget is not None:
            widget.destroy()

    # Re-fetch every source.
    header_data = _get_header_data()
    events_data = _get_events_data()
    stocks_data = _get_stocks_data()
    headlines_data = _get_headlines_data()

    # Header: rebuild and slot above the middle row.
    header, clock_label = _build_header(
        state["root"],
        header_data["greeting"],
        header_data["time_str"],
        header_data["weather"],
    )
    header.pack(fill="x", padx=16, pady=(16, 8), before=state["middle"])
    state["header"] = header
    state["clock_label"] = clock_label

    # Middle row: today + watchlist live inside the existing `middle` frame.
    today_panel = _build_today_panel(state["middle"], events_data)
    today_panel.pack(side=tk.LEFT, fill="both", expand=True, padx=(0, 8))
    state["today_panel"] = today_panel

    watchlist_panel = _build_watchlist_panel(state["middle"], stocks_data)
    watchlist_panel.pack(side=tk.LEFT, fill="both", expand=True, padx=(8, 0))
    state["watchlist_panel"] = watchlist_panel

    # News: full-width below middle, above the button bar.
    news_panel = _build_news_panel(state["root"], headlines_data)
    news_panel.pack(
        fill="both", expand=True, padx=16, pady=8,
        before=state["button_bar"],
    )
    state["news_panel"] = news_panel

    print("[dashboard] Refresh complete.")


# ========== ENTRY POINT ==========
def launch_dashboard():
    root = tk.Tk()
    root.title("FRIDAY")
    root.configure(bg=BG)
    root.geometry("900x650")
    root.minsize(700, 500)

    # Center on screen
    root.update_idletasks()
    w, h = 900, 650
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 2
    root.geometry(f"{w}x{h}+{x}+{y}")

    print("[dashboard] Loading data...")
    header_data = _get_header_data()
    events_data = _get_events_data()
    stocks_data = _get_stocks_data()
    headlines_data = _get_headlines_data()
    print("[dashboard] Data loaded — launching window.")

    state: Dict = {
        "root": root,
        "header": None,
        "middle": None,
        "today_panel": None,
        "watchlist_panel": None,
        "news_panel": None,
        "button_bar": None,
        "clock_label": None,
    }

    header, clock_label = _build_header(
        root, header_data["greeting"], header_data["time_str"], header_data["weather"]
    )
    header.pack(fill="x", padx=16, pady=(16, 8))
    state["header"] = header
    state["clock_label"] = clock_label

    middle = tk.Frame(root, bg=BG)
    middle.pack(fill="both", expand=False, padx=16, pady=8)
    state["middle"] = middle

    today_panel = _build_today_panel(middle, events_data)
    today_panel.pack(side=tk.LEFT, fill="both", expand=True, padx=(0, 8))
    state["today_panel"] = today_panel

    watchlist_panel = _build_watchlist_panel(middle, stocks_data)
    watchlist_panel.pack(side=tk.LEFT, fill="both", expand=True, padx=(8, 0))
    state["watchlist_panel"] = watchlist_panel

    news_panel = _build_news_panel(root, headlines_data)
    news_panel.pack(fill="both", expand=True, padx=16, pady=8)
    state["news_panel"] = news_panel

    button_bar = _build_button_bar(
        root,
        on_refresh=lambda: _refresh_dashboard(state),
        on_briefing=lambda: print("[dashboard] Run Briefing clicked"),
    )
    button_bar.pack(fill="x", padx=16, pady=(8, 16))
    state["button_bar"] = button_bar

    # Kick off the live clock — it self-schedules every second.
    _tick_clock(state)

    root.mainloop()


if __name__ == "__main__":
    launch_dashboard()
