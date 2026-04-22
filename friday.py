"""
FRIDAY — Personal AI Assistant
Entry point.

Phase 1: just greets you out loud on launch.
Later phases will add scheduling, dashboard, hotkeys, etc.

Usage:
    python friday.py          # Normal launch
    python friday.py --now    # (Future) force-run daily briefing
"""

import argparse
import sys

from modules.greeting import greet


def main() -> int:
    parser = argparse.ArgumentParser(description="FRIDAY — Personal AI Assistant")
    parser.add_argument(
        "--now",
        action="store_true",
        help="Force-run the daily briefing immediately (not wired up yet).",
    )
    args = parser.parse_args()

    print("=" * 50)
    print("  FRIDAY  —  booting up")
    print("=" * 50)

    # Launch greeting
    greet()

    if args.now:
        print("[friday] --now flag detected. Daily briefing not wired up yet (Phase 5).")

    print("[friday] Phase 1 complete. Greeting delivered.")
    print("[friday] (Later phases will keep the app running — for now it exits.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())