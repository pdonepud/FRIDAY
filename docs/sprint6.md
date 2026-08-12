# Sprint 6 Plan — FRIDAY

**Product:** FRIDAY (Personal AI Assistant)
**Team:** Solo — Preetam Donepudi
**Sprint completion date:** Planned — June 15, 2026
**Revision:** 1.0 — August 11, 2026

---

## Goal

Ship the full JARVIS-style visual experience. Sprint 5 stood up the Tauri shell, the FastAPI transport, and the first two panels (weather + calendar). Sprint 6 fills in the rest: watchlist and news panels, an hourly forecast strip on the weather card, a center HUD with animated circular arcs (the JARVIS signature visual), and finally — the ergonomics fix — Tauri auto-starting the FastAPI server on window open so the user never has to launch two processes.

By the end of this sprint the dashboard should be demoable without a terminal window visible.

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

### Additional Panels (3 pts)

*Watchlist and news cards wired to the existing FastAPI endpoints.*

| Task | Estimate |
|------|----------|
| Watchlist panel: fetch `/stocks`, render ticker rows with % change and sparkline | 1.5 hr |
| News panel: fetch `/news`, render categorized headline stack (politics/world/markets/tech) | 1 hr |
| Loading + error states, consistent card chrome across all four panels | 0.5 hr |

**Total: ~3 hrs**

### Weather Detail (2 pts)

*Hourly forecast strip beneath the current-conditions block on the weather card.*

| Task | Estimate |
|------|----------|
| Extend `/weather` endpoint to return next 12 hours from Open-Meteo | 0.5 hr |
| Frontend hourly strip: temp + condition glyph per hour, horizontal scroll | 1 hr |
| Match hourly strip typography to existing card design | 0.5 hr |

**Total: ~2 hrs**

### Center HUD (3 pts)

*The JARVIS signature — animated concentric arcs in the dashboard center as an ambient system-status visual.*

| Task | Estimate |
|------|----------|
| SVG concentric arcs, three rings, subtle rotation via CSS animation | 1.5 hr |
| Inner ring: live clock centered inside the arcs | 0.75 hr |
| Middle ring: current CPU / memory sample (via a `/system` endpoint or JS-side) | 0.75 hr |

**Total: ~3 hrs**

### Server Auto-Start (2 pts)

*Tauri launches FastAPI as a sidecar on window open — one process from the user's perspective.*

| Task | Estimate |
|------|----------|
| Configure Tauri sidecar in `tauri.conf.json` pointing at a bundled Python launcher | 1 hr |
| Graceful shutdown: on window close, terminate the FastAPI subprocess | 0.5 hr |
| Retry logic: frontend waits up to 5s for `/health` before rendering panels | 0.5 hr |

**Total: ~2 hrs**

---

## Initial Task Assignment

Solo project — all tasks assigned to Preetam.

---

## Sprint Retrospective

*Not yet started — this sprint is in the backlog.*

---

## Burnup Chart

Will be generated at sprint close.
