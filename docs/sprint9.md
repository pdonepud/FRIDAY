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

### Privacy Design (2 pts) — MUST COMPLETE BEFORE ANY OTHER WELLNESS WORK

*Before storing or transmitting any wellness data, define the privacy contract.*

| Task | Estimate |
|------|----------|
| Local storage only by default: sleep/mood data written to `data/wellness.json`, never transmitted | 0.5 hr |
| Retention policy: configurable, default 90 days rolling window | 0.5 hr |
| Explicit opt-in for external transmission: separate config flag `ALLOW_WELLNESS_TO_LLM = False` (default off) | 0.5 hr |
| Redaction: if wellness data IS sent to Gemini, aggregate/blur specifics (e.g., "poor sleep this week" not "slept 3.5 hours Monday") | 0.5 hr |
| Deletion: `python friday.py --wipe-wellness` command that clears local data | 0.5 hr |

**Total: ~2.5 hrs**

**Design principle:** Wellness data is more sensitive than calendar or news data. Even for a solo project used by one person (the developer), the design shows awareness of privacy engineering. Recruiters reviewing FRIDAY's code will see explicit privacy controls — a strong signal.

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
