"""
FRIDAY — Personal AI Assistant
Entry point.

Usage:
    python friday.py                          # Normal launch with greeting
    python friday.py --ask "your question"    # Quick Q&A mode
    python friday.py --now                    # Force-run daily briefing (Phase 5)
"""

import argparse
import sys

from modules.greeting import greet
from modules.qa import answer


def main() -> int:
    parser = argparse.ArgumentParser(description="FRIDAY — Personal AI Assistant")
    parser.add_argument(
        "--ask",
        type=str,
        default=None,
        help="Quick Q&A mode: ask FRIDAY a question and exit.",
    )
    parser.add_argument(
        "--now",
        action="store_true",
        help="Force-run the daily briefing immediately (not wired up yet).",
    )
    args = parser.parse_args()

    print("=" * 50)
    print("  FRIDAY  —  booting up")
    print("=" * 50)

    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    # Quick Q&A mode — skip the greeting and answer immediately.
    if args.ask:
        answer(args.ask)
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
