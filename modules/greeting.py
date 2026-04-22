"""
greeting.py — Randomized launch lines.

A pool of funny/casual greetings. One is picked at random every launch
so it never gets stale. {name} gets substituted with config.USER_NAME.

Test directly:
    python modules/greeting.py
"""

import os
import random
import sys
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import USER_NAME
from modules.voice import speak


GREETINGS = [
    "What's up boss, welcome back {name} — let's get it.",
    "FRIDAY online. {name}, good to see you again.",
    "Systems up, {name}. What are we building today?",
    "Hey {name}. The world kept spinning while you were gone, don't worry.",
    "Boot complete. Nice to have you back, {name}.",
    "{name}, reporting for duty. Let's make today count.",
    "Oh look who's back. {name}, I missed you. Kind of.",
    "FRIDAY here. {name}, your assistant has entered the chat.",
    "Hey {name}, I've been thinking — but that's a story for later.",
    "Loaded and caffeinated. Well, I would be if I drank coffee. Hi {name}.",
]

TIME_GREETINGS = {
    "morning":   "Good morning {name}. Let's start strong.",
    "afternoon": "Good afternoon {name}. Halfway there.",
    "evening":   "Evening {name}. Let's wind down right.",
    "night":     "It's late, {name}. Hope this is worth it.",
}


def _time_bucket() -> str:
    h = datetime.now().hour
    if 5 <= h < 12:  return "morning"
    if 12 <= h < 17: return "afternoon"
    if 17 <= h < 22: return "evening"
    return "night"


def pick_greeting() -> str:
    """
    Returns a greeting string.
    20% chance of a time-of-day greeting, 80% from the general pool.
    """
    if random.random() < 0.2:
        template = TIME_GREETINGS[_time_bucket()]
    else:
        template = random.choice(GREETINGS)
    return template.format(name=USER_NAME)


def greet() -> None:
    """Pick a greeting and speak it."""
    line = pick_greeting()
    print(f"[greeting] {line}")
    speak(line)


if __name__ == "__main__":
    greet()