"""
calendar_api.py — FRIDAY's calendar reader.

Pulls events from every calendar the user has access to (primary +
subscribed + shared), normalizes the messy Google response into clean
dicts, and provides a few convenience helpers for "today", "next event",
and a spoken-friendly day summary.

Auth is delegated to modules.google_auth.get_calendar_service(), which
handles the one-time browser dance and token caching.

Test directly:
    python modules/calendar_api.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.google_auth import get_calendar_service


_DESCRIPTION_MAX = 500
_NEXT_EVENT_HORIZON_DAYS = 7

# ANSI colors
_GRAY = "\033[90m"
_RESET = "\033[0m"


def _to_rfc3339(dt: datetime) -> str:
    """
    Format a datetime for Google's timeMin/timeMax params.

    Google requires an ISO 8601 / RFC 3339 string with a timezone offset.
    Naive datetimes are assumed to be in the system's local timezone.
    """
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.isoformat()


def _parse_event_time(event_time: Optional[dict]) -> Optional[datetime]:
    """
    Parse a Google event start/end block.

    Google returns either:
        {"dateTime": "2026-04-29T14:00:00-07:00"}  (timed event)
        {"date":     "2026-04-29"}                 (all-day event)

    All-day events are anchored at midnight local time.
    """
    if not event_time:
        return None

    if "dateTime" in event_time:
        try:
            return datetime.fromisoformat(event_time["dateTime"])
        except ValueError:
            return None

    if "date" in event_time:
        try:
            naive_midnight = datetime.fromisoformat(event_time["date"])
        except ValueError:
            return None
        return naive_midnight.astimezone()  # attach local tz

    return None


def _normalize_event(event: dict, calendar_name: str) -> Optional[Dict]:
    """Map a raw Google event dict to FRIDAY's clean event shape."""
    start_raw = event.get("start", {}) or {}
    end_raw = event.get("end", {}) or {}

    start = _parse_event_time(start_raw)
    end = _parse_event_time(end_raw)
    if start is None or end is None:
        return None

    all_day = "date" in start_raw
    description = (event.get("description") or "")[:_DESCRIPTION_MAX]
    attendees = [
        a.get("email", "")
        for a in (event.get("attendees") or [])
        if a.get("email")
    ]

    return {
        "id":            event.get("id", ""),
        "title":         event.get("summary") or "(no title)",
        "start":         start,
        "end":           end,
        "all_day":       all_day,
        "location":      event.get("location") or "",
        "description":   description,
        "attendees":     attendees,
        "calendar_name": calendar_name,
        "url":           event.get("htmlLink") or "",
    }


def get_events(
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    max_results_per_calendar: int = 20,
) -> List[Dict]:
    """
    Fetch events from every accessible calendar within [start, end].

    Defaults: start = now, end = end of today (23:59:59 local).
    Recurring events are expanded (singleEvents=True).

    One bad calendar (permission glitch, etc.) is logged and skipped — it
    doesn't kill the whole call.
    """
    if start is None:
        start = datetime.now().astimezone()
    if end is None:
        end = datetime.now().astimezone().replace(
            hour=23, minute=59, second=59, microsecond=0
        )

    service = get_calendar_service()
    calendars = service.calendarList().list().execute().get("items", [])

    all_events: List[Dict] = []
    for cal in calendars:
        cal_id = cal.get("id")
        cal_name = cal.get("summary", "(unnamed calendar)")
        if not cal_id:
            continue

        try:
            result = service.events().list(
                calendarId=cal_id,
                timeMin=_to_rfc3339(start),
                timeMax=_to_rfc3339(end),
                singleEvents=True,
                orderBy="startTime",
                maxResults=max_results_per_calendar,
            ).execute()
        except Exception as e:
            print(f"[calendar] Skipped {cal_name}: {e}")
            continue

        for ev in result.get("items", []):
            normalized = _normalize_event(ev, cal_name)
            if normalized is not None:
                all_events.append(normalized)

    all_events.sort(key=lambda e: e["start"])
    return all_events


def get_today() -> List[Dict]:
    """Events from now through end of today (local)."""
    return get_events()


def get_next_event() -> Optional[Dict]:
    """
    The single next upcoming event within the next 7 days.

    Returns None if nothing is scheduled in that window.
    """
    now = datetime.now().astimezone()
    horizon = now + timedelta(days=_NEXT_EVENT_HORIZON_DAYS)
    events = get_events(start=now, end=horizon)
    return events[0] if events else None


def get_week() -> List[Dict]:
    """Events from now through the next 7 days."""
    now = datetime.now().astimezone()
    horizon = now + timedelta(days=_NEXT_EVENT_HORIZON_DAYS)
    return get_events(start=now, end=horizon)


def _format_time(dt: datetime, all_day: bool = False) -> str:
    """12-hour clock, no leading zero on hour, drop ':00' minutes."""
    if all_day:
        return "all day"
    hour = dt.hour
    minute = dt.minute
    am_pm = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    if minute == 0:
        return f"{h12} {am_pm}"
    return f"{h12}:{minute:02d} {am_pm}"


def _format_delta(target: datetime) -> str:
    """
    Human time-until string: 'in 5m', '1h 23m', 'in 2 days', or 'now'.
    """
    now = datetime.now().astimezone()
    delta = target - now
    total_seconds = delta.total_seconds()

    if total_seconds <= 0:
        return "now"

    days = delta.days
    if days >= 1:
        return f"in {days} day{'s' if days != 1 else ''}"

    total_minutes = int(total_seconds // 60)
    hours = total_minutes // 60
    minutes = total_minutes % 60

    if hours >= 1:
        return f"in {hours}h {minutes}m"
    return f"in {minutes}m"


def describe_today() -> str:
    """One spoken-friendly paragraph summarizing today's remaining events."""
    events = get_today()

    if not events:
        return "You have nothing on the calendar for the rest of today."

    if len(events) == 1:
        e = events[0]
        if e["all_day"]:
            return f"You have one thing today: {e['title']} all day."
        return f"You have one thing today: {e['title']} at {_format_time(e['start'])}."

    if len(events) <= 3:
        count_word = {2: "two", 3: "three"}[len(events)]
        parts: List[str] = []
        for i, e in enumerate(events):
            connector = "" if i == 0 else "then "
            if e["all_day"]:
                parts.append(f"{connector}{e['title']} all day")
            else:
                parts.append(f"{connector}{e['title']} at {_format_time(e['start'])}")
        return f"You have {count_word} things today. " + ", ".join(parts) + "."

    nxt = events[0]
    if nxt["all_day"]:
        next_phrase = f"{nxt['title']} all day"
    else:
        next_phrase = f"{nxt['title']} at {_format_time(nxt['start'])}"
    return f"You have a packed day — {len(events)} events. Up next: {next_phrase}."


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    try:
        events = get_today()
        nxt = get_next_event()

        print("=== Today's Events ===")
        print()
        if not events:
            print("(nothing on the calendar for the rest of today)")
        else:
            for e in events:
                time_str = _format_time(e["start"], e["all_day"])
                title = e["title"]
                if len(title) > 35:
                    title = title[:32] + "..."
                print(
                    f"{time_str:>8}  {title:<35}  "
                    f"{_GRAY}({e['calendar_name']}){_RESET}"
                )
        print()

        print("=== Next Event ===")
        if nxt is None:
            print("(nothing in the next 7 days)")
        else:
            print(f"{nxt['title']} {_format_delta(nxt['start'])}")
        print()

        print("=== Spoken Summary ===")
        print(describe_today())

    except FileNotFoundError as e:
        print(f"[calendar] Auth not set up: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[calendar] Failure: {type(e).__name__}: {e}")
        sys.exit(1)
