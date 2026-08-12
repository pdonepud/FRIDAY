# Sprint 5 Plan — FRIDAY

**Product:** FRIDAY (Personal AI Assistant)
**Team:** Solo — Preetam Donepudi
**Sprint completion date:** In progress — target May 31, 2026
**Revision:** 1.0 — August 11, 2026

---

## Goal

Pay down known tech debt from Sprint 4 and rearchitect the UI stack for the next year of growth. Two threads run in parallel this sprint: first, quick reliability wins (kill the Alpha Vantage rate limits, refresh the README, tidy the repo); second — and the bigger bet — abandoning the planned tkinter dashboard entirely in favor of a Tauri (Rust + web) shell backed by a FastAPI HTTP layer.

The Tauri pivot is the architectural inflection point for the whole project. It unlocks a real JARVIS-style visual experience, decouples the UI from the Python core (any web frontend can now consume the modules over HTTP), and turns FRIDAY from a CLI curiosity into something demoable.

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

### Stocks Migration (2 pts) ✅

*Replace Alpha Vantage with yfinance — no rate limits, no API key, dynamic cache TTL keyed to market hours.*

| Task | Estimate |
|------|----------|
| Swap `modules/stocks.py` internals to `yfinance.Ticker` (`48c69e`) | 1 hr |
| Dynamic cache TTL — short during market hours, long after close | 0.5 hr |
| Drop `ALPHA_VANTAGE_API_KEY` from config template | 0.25 hr |
| Smoke test watchlist end-to-end | 0.25 hr |

**Total: ~2 hrs**

### Documentation Refresh (2 pts) ✅

*README and repo hygiene reflecting the current architecture (tkinter deprecated, Tauri incoming).*

| Task | Estimate |
|------|----------|
| Rewrite README sections: tech stack, running the app, roadmap (`48c69e`) | 1 hr |
| Mark tkinter dashboard as deprecated; document the Tauri pivot | 0.5 hr |
| Add `CASE_STUDY.md` to `.gitignore` while it's still private (`5368f99`) | 0.25 hr |
| Verify `config.example.py` matches `config.py` structure | 0.25 hr |

**Total: ~2 hrs**

### Tauri Scaffold (3 pts) ✅

*Rust + vanilla web frontend, JARVIS boot screen rendering — the foundation for every future UI iteration.*

| Task | Estimate |
|------|----------|
| `cargo tauri init` inside `ui/`, minimal `tauri.conf.json` | 0.5 hr |
| Skeleton `index.html` + `main.js` + `style.css` in `ui/src/` | 1 hr |
| First-pass boot screen renders in Tauri window (`c08b8e1`) | 1 hr |
| Document dev loop (`cargo tauri dev`) in README | 0.5 hr |

**Total: ~3 hrs**

### FastAPI HTTP Layer (3 pts) ✅

*Wrap every module as a REST endpoint on port 8765 so the Tauri frontend (or any client) can consume them.*

| Task | Estimate |
|------|----------|
| Scaffold `server/api.py` with FastAPI app on port 8765 (`e8a81b1`) | 1 hr |
| Expose endpoints: `/api/weather`, `/api/calendar/today`, `/api/calendar/next`, `/api/stocks`, `/api/news`, `/api/briefing`, `/api/ask` | 1.5 hr |
| CORS config for localhost Tauri origin | 0.25 hr |
| Manual test each endpoint with `curl` | 0.25 hr |

**Total: ~3 hrs**

### Boot Animation (2 pts) ✅

*JARVIS-style letter-reveal boot sequence — cycling status lines, then fade into the dashboard.*

| Task | Estimate |
|------|----------|
| CSS keyframes for letter-by-letter reveal | 0.75 hr |
| JS-driven status cycling: "INITIALIZING SYSTEMS" → "ESTABLISHING SECURE CONNECTION" → "ONLINE — WELCOME, PREETAM" | 0.75 hr |
| Fade transition into dashboard placeholder (`ad9ef40`) | 0.5 hr |

**Total: ~2 hrs**

### First Data Panels (2 pts) 🚧 In progress

*Wire the weather and calendar panels to the live FastAPI endpoints (Phase 13.3.B).*

| Task | Estimate |
|------|----------|
| Weather panel: fetch `/weather`, render current + condition icon | 0.75 hr |
| Calendar panel: fetch `/calendar/today`, render event list with time strip | 1 hr |
| Loading + error states for both panels | 0.25 hr |

**Total: ~2 hrs**

### Briefing Button (1 pt) 📋 Planned

*Add a play button on the frontend that triggers `/briefing` and streams the audio playback via the Python voice layer (Phase 13.3.C).*

| Task | Estimate |
|------|----------|
| Frontend button → POST `/briefing` | 0.25 hr |
| Backend triggers `speak_interruptible()` in a thread; returns immediately | 0.5 hr |
| Frontend "playing…" state polling `/briefing/status` (Esc still stops locally) | 0.25 hr |

**Total: ~1 hr**

---

## Initial Task Assignment

Solo project — all tasks assigned to Preetam.

---

## Sprint Retrospective

*In progress — retrospective will be written at sprint close.*

---

## Burnup Chart

Will be generated at sprint close. Target file: `docs/sprint_reports/sprint5_burnup.png`. *(Chart generation pending — Sprint 7 deliverable.)*
