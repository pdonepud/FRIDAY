"""
api.py — FRIDAY's HTTP API.

Thin FastAPI wrapper that exposes the existing `modules/` functions as REST
endpoints. The Tauri frontend (and any other client) calls these to fetch
data. This file does NOT re-implement any logic — it just calls the module
functions and returns their output as JSON.

Run standalone:
    python -m server.api

Then visit:
    http://127.0.0.1:8765/docs   (interactive Swagger explorer)
    http://127.0.0.1:8765/api/health
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import threading
from datetime import datetime
from typing import Any, Dict, List, Optional
from typing import Optional as _Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import USER_NAME
from modules.briefing import generate_briefing
from modules.calendar_api import describe_today, get_next_event, get_today
from modules.gemini import ask as gemini_ask
from modules.news import get_balanced_headlines
from modules.stocks import get_watchlist
from modules.voice import speak_interruptible
from modules.weather import get_weather


# ========== APP ==========
app = FastAPI(
    title="FRIDAY API",
    description="HTTP interface to FRIDAY's modules — used by the Tauri frontend.",
    version="0.1.0",
)

# Tauri runs at tauri://localhost (or http://tauri.localhost on some platforms)
# and this server runs at http://127.0.0.1:8765. Without CORS, the webview
# blocks cross-origin requests. Wide-open is fine for local dev; tighten on
# distribution.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ========== BRIEFING PLAYBACK STATE ==========
# Single-instance briefing state — only one briefing can play at a time.
# Frontend polls /api/briefing/status; Python speaks via existing speak_interruptible.
_briefing_lock = threading.RLock()
_briefing_state = {
    "status": "idle",         # idle | generating | speaking | stopped | done | error
    "message": "",            # human-readable status detail
    "started_at": None,       # ISO timestamp of last start
    "text": None,             # last briefing text (for display, not audio)
}


def _set_briefing_state(**updates):
    """Thread-safe update of the briefing state singleton."""
    with _briefing_lock:
        _briefing_state.update(updates)


def _get_briefing_state() -> dict:
    """Thread-safe snapshot of the briefing state singleton."""
    with _briefing_lock:
        return dict(_briefing_state)


# ========== RESPONSE MODELS ==========
class HealthResponse(BaseModel):
    status: str
    user: str
    timestamp: str


class HourlyForecast(BaseModel):
    time: str                       # naive-local ISO from Open-Meteo (timezone=auto)
    temp_f: float
    weather_code: int               # WMO code, translated to icon on the frontend
    precipitation_probability: int  # 0-100; already coerced from null to 0 upstream
    is_day: bool


class WeatherResponse(BaseModel):
    temp_f: float
    feels_like_f: float
    conditions: str
    humidity: int
    wind_mph: float
    is_day: bool
    today_high_f: float
    today_low_f: float
    rain_chance_today: int
    tomorrow_high_f: float
    tomorrow_low_f: float
    tomorrow_conditions: str
    rain_chance_tomorrow: int
    hourly: List[HourlyForecast] = []   # default empty preserves the endpoint's
                                        # existing contract if the upstream
                                        # hourly block ever comes back malformed


class StockQuote(BaseModel):
    ticker: str
    price: float
    change: float
    change_pct: float
    stale: bool


class CalendarEvent(BaseModel):
    id: str
    title: str
    start: str       # ISO format
    end: str
    all_day: bool
    location: str
    description: str
    attendees: List[str]
    calendar_name: str
    url: str


class Headline(BaseModel):
    title: str
    source: str
    description: str
    url: str
    category: str
    topic_label: str
    stale: bool


class BriefingResponse(BaseModel):
    text: str
    word_count: int


class BriefingStatusResponse(BaseModel):
    status: str          # idle | generating | speaking | stopped | done | error
    message: str
    started_at: Optional[str] = None
    text: Optional[str] = None


class BriefingSpeakResponse(BaseModel):
    accepted: bool
    message: str


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    question: str
    answer: str


# ========== ENDPOINTS ==========
@app.get("/api/health", response_model=HealthResponse)
def health():
    """Lightweight health check — used by the frontend to confirm the server is reachable."""
    return HealthResponse(
        status="ok",
        user=USER_NAME,
        timestamp=datetime.now().isoformat(),
    )


@app.get("/api/weather", response_model=WeatherResponse)
def weather():
    """Current weather + 2-day forecast for the configured lat/long."""
    try:
        data = get_weather()
        return WeatherResponse(**data)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Weather service unavailable: {e}")


@app.get("/api/calendar/today", response_model=List[CalendarEvent])
def calendar_today():
    """All events on today's calendar, merged across all the user's calendars."""
    try:
        events = get_today()
        return [
            CalendarEvent(
                id=e["id"],
                title=e["title"],
                start=e["start"].isoformat(),
                end=e["end"].isoformat(),
                all_day=e["all_day"],
                location=e["location"],
                description=e["description"],
                attendees=e["attendees"],
                calendar_name=e["calendar_name"],
                url=e["url"],
            )
            for e in events
        ]
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Calendar unavailable: {e}")


@app.get("/api/calendar/next", response_model=Optional[CalendarEvent])
def calendar_next():
    """The single next upcoming event (within 7 days). Returns null if nothing scheduled."""
    try:
        event = get_next_event()
        if event is None:
            return None
        return CalendarEvent(
            id=event["id"],
            title=event["title"],
            start=event["start"].isoformat(),
            end=event["end"].isoformat(),
            all_day=event["all_day"],
            location=event["location"],
            description=event["description"],
            attendees=event["attendees"],
            calendar_name=event["calendar_name"],
            url=event["url"],
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Calendar unavailable: {e}")


@app.get("/api/stocks", response_model=List[StockQuote])
def stocks():
    """The configured watchlist with current prices and percent change."""
    try:
        return [StockQuote(**q) for q in get_watchlist()]
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Stocks unavailable: {e}")


@app.get("/api/news", response_model=List[Headline])
def news(
    politics: int = Query(2, ge=0, le=5, description="Number of politics headlines"),
    world: int = Query(1, ge=0, le=5, description="Number of world headlines"),
    markets: int = Query(1, ge=0, le=5, description="Number of market headlines"),
    tech: int = Query(1, ge=0, le=5, description="Number of tech headlines"),
):
    """Top headlines balanced across categories. Quotas configurable via query params."""
    try:
        quotas = {"politics": politics, "world": world, "markets": markets, "tech": tech}
        articles = get_balanced_headlines(quotas)
        return [Headline(**a) for a in articles]
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"News unavailable: {e}")


@app.get("/api/briefing", response_model=BriefingResponse)
def briefing(force: bool = Query(False, description="Bypass cache and force fresh generation")):
    """Generate (or retrieve from cache) the daily briefing text. Does NOT speak it."""
    try:
        text = generate_briefing(speak_aloud=False, force=force)
        return BriefingResponse(text=text, word_count=len(text.split()))
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Briefing failed: {e}")


def _run_briefing_in_background():
    """
    Background worker that generates + speaks the briefing.
    Updates the briefing state singleton at each phase transition.
    Runs on a thread so the HTTP endpoint returns immediately.

    The endpoint reserves the "generating" state before starting this
    thread (so the check-and-reserve is atomic), so we don't set it
    again here — we go straight to generation.
    """
    try:
        text = generate_briefing(speak_aloud=False)
        _set_briefing_state(
            status="speaking",
            message="Speaking. Press Esc to stop.",
            text=text,
        )
        completed = speak_interruptible(text)
        # speak_interruptible returns False if the user pressed Esc mid-playback.
        current = _get_briefing_state()
        if current["status"] == "speaking":
            if completed:
                _set_briefing_state(status="done", message="Briefing complete.")
            else:
                _set_briefing_state(status="stopped", message="Briefing stopped.")
    except Exception as e:
        _set_briefing_state(
            status="error",
            message=f"Briefing failed: {e}",
        )


@app.post("/api/briefing/speak", response_model=BriefingSpeakResponse)
def briefing_speak():
    """
    Start the briefing in a background thread. Returns immediately.
    Frontend polls /api/briefing/status for progress. Only one briefing
    can play at a time — rejects if one is already in flight.
    """
    # Hold the lock across BOTH the "already playing" check AND the
    # transition to "generating" so two concurrent requests can't both
    # observe "idle" and start rival threads. _briefing_lock is an
    # RLock, so the nested acquire inside _set_briefing_state() is safe.
    with _briefing_lock:
        if _briefing_state["status"] in ("generating", "speaking"):
            return BriefingSpeakResponse(
                accepted=False,
                message="A briefing is already playing. Press Esc to stop it first.",
            )
        _set_briefing_state(
            status="generating",
            message="Pulling your briefing together...",
            started_at=datetime.now().isoformat(),
            text=None,
        )
    thread = threading.Thread(target=_run_briefing_in_background, daemon=True)
    thread.start()
    return BriefingSpeakResponse(
        accepted=True,
        message="Briefing started.",
    )


@app.get("/api/briefing/status", response_model=BriefingStatusResponse)
def briefing_status():
    """Snapshot of the current briefing playback state."""
    current = _get_briefing_state()
    return BriefingStatusResponse(**current)


@app.post("/api/ask", response_model=AskResponse)
def ask(req: AskRequest):
    """Send a question to Gemini and return the answer (no voice synthesis)."""
    try:
        answer = gemini_ask(req.question)
        return AskResponse(question=req.question, answer=answer)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Gemini unavailable: {e}")


if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="FRIDAY FastAPI server")
    parser.add_argument(
        "--no-reload",
        action="store_true",
        help=(
            "Disable uvicorn auto-reload. Set by the Tauri sidecar — reload spawns "
            "a worker subprocess whose lifetime the sidecar cannot own, which can "
            "leave orphaned python.exe processes after the app window closes."
        ),
    )
    args = parser.parse_args()

    uvicorn.run(
        "server.api:app",
        host="127.0.0.1",
        port=8765,
        reload=not args.no_reload,
        log_level="info",
    )
