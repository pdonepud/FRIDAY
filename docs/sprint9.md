# Sprint 9 Plan — FRIDAY

**Product:** FRIDAY (Personal AI Assistant)
**Team:** Solo — Preetam Donepudi
**Sprint completion date:** Planned — August 31, 2026
**Revision:** 1.0 — August 11, 2026

---

## Goal

Add the ambient wellness layer. This sprint moves FRIDAY from *reactive* (respond when spoken to) to *proactive* (nudge, notice, adapt). Sleep and mood logging give the assistant longitudinal data to reason over; away-mode + hydration/posture nudges + weather-reactive music make it feel like the assistant is *paying attention* between briefings.

The sprint closes with meeting prep cards and an end-of-day wrap — bookending the daily briefing with pre-meeting talking points in the morning and a reflection summary at night.

---

## Team Roles

| Role | Owner |
|------|-------|
| Product Owner | Preetam (self-directed scope decisions) |
| Tech Lead | Preetam (architectural decisions) |
| Backend Developer | Preetam |
| Frontend Developer | Preetam |
| DevOps / CI | Preetam |
| QA | Preetam (manual testing; unit tests planned for Sprint 7) |
| Scrum Master | Self-managed |

---

## Scrum Times

- **Weekly (Sunday)** — sprint progress review + backlog grooming
- **End of sprint** — retrospective doc + burnup chart
- **Ad-hoc** — architectural decisions logged in `docs/decisions/` as ADRs

---

## Task Listing

### Sleep + Mood Tracker (4 pts)

*Daily logging with Gemini-powered pattern recognition once ~2 weeks of data exists.*

| Task | Estimate |
|------|----------|
| `modules/wellness.py` — JSON log of sleep hours + quality + mood rating per day | 1 hr |
| CLI + hotkey entry point: `friday --log-sleep 7.5 good` / `--log-mood 6` | 1 hr |
| Gemini-backed trend summary: "What patterns do you see in the last 14 days?" | 1.5 hr |
| Dashboard panel: 14-day sleep bars + mood line, minimal chrome | 1 hr |

**Total: ~4.5 hrs**

### Nudges + Away Mode + Weather-Reactive Music (3 pts)

*Ambient behaviors that trigger without direct user input.*

| Task | Estimate |
|------|----------|
| Hydration + posture nudges — configurable interval, respects focus mode | 1 hr |
| Away mode — auto-mute audio + pause nudges after N minutes of no keyboard/mouse | 1 hr |
| Weather-reactive music — pick from `music/` folder based on current conditions | 1 hr |

**Total: ~3 hrs**

### Meeting Prep + End-of-Day Wrap (3 pts)

*Bookend features — pre-meeting talking points from calendar context, evening reflection summary.*

| Task | Estimate |
|------|----------|
| Meeting prep: 15 min before each calendar event, generate 3-bullet prep card via Gemini | 1.5 hr |
| End-of-day wrap: at 6pm local, summarize the day (events attended, briefing gist, wellness log) | 1.5 hr |

**Total: ~3 hrs**

---

## Initial Task Assignment

Solo project — all tasks assigned to Preetam.

---

## Sprint Retrospective

*Not yet started — this sprint is in the backlog.*

---

## Burnup Chart

Will be generated at sprint close.
