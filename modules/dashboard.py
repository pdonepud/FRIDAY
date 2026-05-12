"""
dashboard.py — FRIDAY's tkinter dashboard.

Dark-mode at-a-glance window: header (greeting / time / weather), today's
events, watchlist, top headlines, and action buttons.

Step 6.1: skeleton with hardcoded placeholder data. Real data wiring lands
in step 6.2.

Run standalone:
    python modules/dashboard.py
"""

import os
import sys
import tkinter as tk
from tkinter import ttk
from typing import List, Dict

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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


# ========== PLACEHOLDER DATA (step 6.1) ==========
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
def _build_header(parent: tk.Misc, greeting: str, time_str: str, weather: str) -> tk.Frame:
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

    tk.Label(
        inner, text=time_str, bg=PANEL, fg=TEXT,
        font=("Segoe UI", 14),
    ).pack(side=tk.LEFT)

    tk.Label(
        inner, text="  ·  ", bg=PANEL, fg=TEXT_DIM,
        font=("Segoe UI", 14),
    ).pack(side=tk.LEFT)

    tk.Label(
        inner, text=weather, bg=PANEL, fg=TEXT_DIM,
        font=("Segoe UI", 14),
    ).pack(side=tk.LEFT)

    return frame


def _build_today_panel(parent: tk.Misc, events: List[Dict]) -> tk.Frame:
    frame = _panel(parent)

    _section_header(frame, "Today").pack(fill="x", padx=16, pady=(12, 8))

    if not events:
        tk.Label(
            frame, text="No events today.", bg=PANEL, fg=TEXT_DIM,
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
            frame, text="No tickers configured.", bg=PANEL, fg=TEXT_DIM,
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
            frame, text="No headlines available.", bg=PANEL, fg=TEXT_DIM,
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

    header = _build_header(root, PLACEHOLDER_GREETING, PLACEHOLDER_TIME, PLACEHOLDER_WEATHER)
    header.pack(fill="x", padx=16, pady=(16, 8))

    middle = tk.Frame(root, bg=BG)
    middle.pack(fill="both", expand=False, padx=16, pady=8)

    today_panel = _build_today_panel(middle, PLACEHOLDER_EVENTS)
    today_panel.pack(side=tk.LEFT, fill="both", expand=True, padx=(0, 8))

    watchlist_panel = _build_watchlist_panel(middle, PLACEHOLDER_STOCKS)
    watchlist_panel.pack(side=tk.LEFT, fill="both", expand=True, padx=(8, 0))

    news_panel = _build_news_panel(root, PLACEHOLDER_HEADLINES)
    news_panel.pack(fill="both", expand=True, padx=16, pady=8)

    button_bar = _build_button_bar(
        root,
        on_refresh=lambda: print("[dashboard] Refresh clicked"),
        on_briefing=lambda: print("[dashboard] Run Briefing clicked"),
    )
    button_bar.pack(fill="x", padx=16, pady=(8, 16))

    root.mainloop()


if __name__ == "__main__":
    launch_dashboard()
