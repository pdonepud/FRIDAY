"""
FRIDAY — Personal AI Assistant
Entry point.

Usage:
    python friday.py                          # Normal launch with greeting
    python friday.py --ask "your question"    # Quick Q&A mode
    python friday.py --calendar               # Speak today's schedule
    python friday.py --next                   # Speak the next upcoming event
    python friday.py --now                    # Force-run daily briefing (Phase 5)
"""

import argparse
import sys
from datetime import datetime

from modules.calendar_api import describe_today, get_next_event
from modules.greeting import greet
from modules.qa import answer
from modules.voice import speak


def _format_when(start: datetime) -> str:
    """Spoken-friendly 'how far away' phrase for an upcoming event."""
    now = datetime.now(start.tzinfo)
    delta = start - now
    mins = int(delta.total_seconds() / 60)

    if mins < 60:
        return f"in {mins} minutes"
    if mins < 1440:
        hours = mins // 60
        rem = mins % 60
        if rem:
            return f"in {hours} hours and {rem} minutes"
        return f"in {hours} hours"
    days = mins // 1440
    return f"in {days} day" if days == 1 else f"in {days} days"


def main() -> int:
    parser = argparse.ArgumentParser(description="FRIDAY — Personal AI Assistant")
    parser.add_argument(
        "--ask",
        type=str,
        default=None,
        help="Quick Q&A mode: ask FRIDAY a question and exit.",
    )
    parser.add_argument(
        "--calendar",
        action="store_true",
        help="Speak today's schedule out loud.",
    )
    parser.add_argument(
        "--next",
        action="store_true",
        help="Speak just the next upcoming event.",
    )
    parser.add_argument(
        "--now",
        action="store_true",
        help="Force-run the daily briefing immediately (not wired up yet).",
    )
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    args = parser.parse_args()

    print("=" * 50)
    print("  FRIDAY  —  booting up")
    print("=" * 50)

    # Quick Q&A mode — skip the greeting and answer immediately.
    if args.ask:
        answer(args.ask)
        return 0

    if args.calendar:
        line = describe_today()
        print(f"[FRIDAY] {line}")
        speak(line)
        return 0

    if args.next:
        event = get_next_event()
        if event is None:
            line = "You have nothing on the calendar in the next 7 days."
        else:
            line = f"Your next event is {event['title']}, {_format_when(event['start'])}."
        print(f"[FRIDAY] {line}")
        speak(line)
        return 0

    # Launch greeting
    greet()

    if args.now:
        print("[friday] --now flag detected. Daily briefing not wired up yet (Phase 5).")

    print("[friday] Phase 1 complete. Greeting delivered.")
    print("[friday] (Later phases will keep the app running — for now it exits.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
