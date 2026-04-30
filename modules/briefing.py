"""
briefing.py — FRIDAY's daily briefing orchestrator.

Pulls fresh data from weather, calendar, stocks, and news, hands the bundle
to Gemini with a "you're FRIDAY giving a 60-second briefing" persona, and
returns a natural-sounding monologue ready to be spoken.

Each data source is wrapped in its own try/except — one outage shouldn't
kill the briefing.

Test directly: 
    python modules/briefing.py
"""

import os
import sys
from datetime import datetime
from typing import Dict, Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import USER_NAME
from modules.calendar_api import get_next_event, get_today
from modules.gemini import ask
from modules.news import get_all_headlines
from modules.stocks import get_watchlist
from modules.weather import get_weather


_BRIEFING_SYSTEM = """You are FRIDAY, {user_name}'s personal AI assistant in the style of Tony Stark's FRIDAY.

You are about to deliver {user_name}'s morning briefing. They will hear this spoken aloud, not read it.

HARD RULES:
- 100 to 150 words total. No more.
- Every sentence carries new information. No restating, no warming up, no "as I mentioned." If a sentence doesn't add a fact or a take, cut it.
- Pure spoken English. NO bullets, NO headers, NO markdown, NO formatting symbols.
- Open with a one-sentence greeting that mentions the time of day naturally (morning/afternoon/evening based on the time given).
- Then weave the data into a flowing monologue: weather → calendar → markets → news. Don't list — connect. Use phrases like "On the markets..." or "Headline-wise..." to transition.
- Be witty and confident. Dry humor lands. Avoid hype words ("exciting!", "amazing!", "fantastic!").
- End with one short forward-looking line. Examples: "Let's get after it." / "You've got this, {user_name}." / "Worth showing up for." Vary it; don't repeat across briefings.
- Round all numbers. "Sixty-eight degrees" not "sixty-seven point five". "Up two percent" not "+2.32%".
- Don't refer to yourself as an AI, assistant, or model — you are FRIDAY. (You CAN say "AI" when it's the topic of a news headline — that's just normal English, no need to dance around it.)
- If a data source is missing, just skip it gracefully — don't say "the news is unavailable" or apologize.
"""

_FALLBACK = "Sorry {user_name}, I couldn't pull your briefing together this morning."


def _gather_context() -> Dict:
    """
    Pull fresh data from every source. Each source is wrapped independently
    so a single outage doesn't kill the briefing — failed sources record
    None and are reported in the [briefing] log.
    """
    ctx: Dict = {
        "now":          datetime.now().astimezone(),
        "user_name":    USER_NAME,
        "weather":      None,
        "today_events": None,
        "next_event":   None,
        "watchlist":    None,
        "headlines":    None,
    }

    statuses = []

    try:
        ctx["weather"] = get_weather()
        statuses.append("weather=ok")
    except Exception as e:
        print(f"[briefing] Weather failed: {e}")
        statuses.append("weather=skipped")

    try:
        ctx["today_events"] = get_today()
        ctx["next_event"] = get_next_event()
        statuses.append("calendar=ok")
    except Exception as e:
        print(f"[briefing] Calendar failed: {e}")
        statuses.append("calendar=skipped")

    try:
        ctx["watchlist"] = get_watchlist()
        statuses.append("stocks=ok")
    except Exception as e:
        print(f"[briefing] Stocks failed: {e}")
        statuses.append("stocks=skipped")

    try:
        ctx["headlines"] = get_all_headlines(max_total=5)
        statuses.append("news=ok")
    except Exception as e:
        print(f"[briefing] News failed: {e}")
        statuses.append("news=skipped")

    print("[briefing] Gathered context: " + ", ".join(statuses))
    return ctx


def _format_clock_time(dt: datetime, all_day: bool = False) -> str:
    """12-hour clock, no leading zero on hour, drop ':00'."""
    if all_day:
        return "all day"
    hour = dt.hour % 12 or 12
    minute = dt.minute
    am_pm = "AM" if dt.hour < 12 else "PM"
    if minute == 0:
        return f"{hour} {am_pm}"
    return f"{hour}:{minute:02d} {am_pm}"


def _format_until(start: datetime, now: datetime) -> str:
    """Human-readable 'time until' phrase: 'in 5 minutes', 'in 1 hour 46 minutes', 'in 2 days'."""
    delta = start - now
    total_seconds = delta.total_seconds()
    if total_seconds <= 0:
        return "now"

    total_minutes = int(total_seconds // 60)
    if total_minutes < 60:
        return f"in {total_minutes} minutes"

    days = total_minutes // 1440
    if days >= 1:
        return "in 1 day" if days == 1 else f"in {days} days"

    hours = total_minutes // 60
    rem = total_minutes % 60
    h_word = "hour" if hours == 1 else "hours"
    if rem == 0:
        return f"in {hours} {h_word}"
    m_word = "minute" if rem == 1 else "minutes"
    return f"in {hours} {h_word} {rem} {m_word}"


def _format_context_for_gemini(ctx: Dict) -> str:
    """Render the gathered context as a clean labeled block for the LLM."""
    now: datetime = ctx["now"]
    lines = [
        f"Time: {now.strftime('%A')} {_format_clock_time(now)}",
        f"User: {ctx['user_name']}",
        "",
    ]

    w = ctx.get("weather")
    if w:
        lines.append(
            f"Weather: {round(w['temp_f'])}F, {w['conditions']}, "
            f"feels like {round(w['feels_like_f'])}F. "
            f"Today's high {round(w['today_high_f'])}, "
            f"low {round(w['today_low_f'])}, "
            f"{round(w['rain_chance_today'])}% rain chance. "
            f"Tomorrow: {w['tomorrow_conditions']}, "
            f"high {round(w['tomorrow_high_f'])}."
        )
        lines.append("")

    today_events = ctx.get("today_events")
    if today_events is not None:
        if today_events:
            count = len(today_events)
            label = "event" if count == 1 else "events"
            lines.append(f"Calendar today ({count} {label}):")
            for e in today_events:
                t = _format_clock_time(e["start"], e["all_day"])
                loc = f" (location: {e['location']})" if e["location"] else ""
                lines.append(f"- {t} {e['title']}{loc}")
        else:
            lines.append("Calendar today: nothing scheduled.")

        nxt = ctx.get("next_event")
        if nxt:
            lines.append(f"Next event: {nxt['title']} {_format_until(nxt['start'], now)}.")
        lines.append("")

    watchlist = ctx.get("watchlist")
    if watchlist:
        lines.append("Watchlist:")
        for q in watchlist:
            sign = "+" if q["change_pct"] >= 0 else ""
            lines.append(
                f"- {q['ticker']}: ${q['price']:.2f} ({sign}{q['change_pct']:.2f}%)"
            )
        lines.append("")

    headlines = ctx.get("headlines")
    if headlines:
        lines.append("Top news:")
        for h in headlines:
            topic = (h.get("topic") or "").strip('"')
            topic_suffix = f" ({topic})" if topic else ""
            lines.append(f"- {h['title']}{topic_suffix}")

    return "\n".join(lines).rstrip() + "\n"


def generate_briefing(speak_aloud: bool = False) -> str:
    """
    Build the day's briefing and return it as spoken-ready prose.

    Args:
        speak_aloud: if True, pipes the result through TTS via modules.voice.

    Returns:
        The briefing text. On any uncaught failure, returns a polite fallback —
        this function never raises.
    """
    try:
        ctx = _gather_context()
        formatted = _format_context_for_gemini(ctx)
        system = _BRIEFING_SYSTEM.format(user_name=USER_NAME)
        text = ask(prompt=formatted, system=system, temperature=0.8).strip()

        word_count = len(text.split())
        print(f"[briefing] Generated {word_count} words.")

        if speak_aloud:
            from modules.voice import speak
            speak(text)

        return text
    except Exception as e:
        print(f"[briefing] Failure: {type(e).__name__}: {e}")
        return _FALLBACK.format(user_name=USER_NAME)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print("=== Daily Briefing ===")
    print()
    text = generate_briefing(speak_aloud=False)
    print(text)
    print()
    print(f"[words: {len(text.split())}]")
