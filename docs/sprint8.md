# Sprint 8 Plan — FRIDAY

**Product:** FRIDAY (Personal AI Assistant)
**Team:** Solo — Preetam Donepudi
**Sprint completion date:** Planned — July 31, 2026
**Revision:** 1.0 — August 11, 2026

---

## Goal

Extend FRIDAY beyond the briefing into always-on productivity territory. Two features anchor the sprint: system-wide global hotkeys so the assistant is reachable without alt-tabbing to the dashboard, and a focus mode that combines a Pomodoro timer with lightweight site blocking.

These are the features that convert FRIDAY from "morning briefing tool" into "companion that lives on the machine all day."

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

### Global Hotkeys (5 pts)

*System-wide keyboard shortcuts for the five most common FRIDAY actions, with a clipboard-aware AI trigger.*

| Task | Estimate |
|------|----------|
| Register `Ctrl+Alt+F` (wake / Q&A prompt overlay) via `keyboard` library | 1 hr |
| Register `Ctrl+Alt+C` (clipboard AI — summarize / translate / rewrite selection) | 1.5 hr |
| Register `Ctrl+Alt+N` ("what should I do right now?" context-aware suggestion) | 1 hr |
| Register `Ctrl+Alt+M` (toggle focus mode) and `Ctrl+Alt+Q` (quit FRIDAY) | 0.5 hr |
| Background service pattern — FRIDAY runs as a tray app after Tauri window closes | 1.5 hr |

**Total: ~5.5 hrs**

### Focus Mode + Pomodoro (4 pts)

*25/5 Pomodoro cycles with configurable site blocklist and optional lo-fi background music.*

| Task | Estimate |
|------|----------|
| `modules/focus.py` — 25-min work / 5-min break state machine | 1 hr |
| Site blocker via hosts file edits (with restore-on-exit safety net) | 2 hr |
| Optional lo-fi playback during focus sessions (pygame + local `music/` folder) | 0.75 hr |
| Notification silencing hook — Windows Focus Assist toggle | 0.75 hr |

**Total: ~4.5 hrs**

---

## Initial Task Assignment

Solo project — all tasks assigned to Preetam.

---

## Sprint Retrospective

*Not yet started — this sprint is in the backlog.*

---

## Burnup Chart

Will be generated at sprint close.
