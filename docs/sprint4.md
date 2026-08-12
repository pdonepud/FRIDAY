# Sprint 4 Plan — FRIDAY

**Product:** FRIDAY (Personal AI Assistant)
**Team:** Solo — Preetam Donepudi
**Sprint completion date:** April 30, 2026
**Revision:** 1.0 — August 11, 2026

> *This sprint doc was written retrospectively in August 2026 to document work completed during this period. The Scrum-style sprint framing was adopted at the start of Sprint 5 to formalize the project structure going forward. Task estimates are based on actual time recorded in git history.*

---

## Goal

Combine everything shipped in Sprints 1–3 into the flagship experience: a spoken 250–350 word daily briefing that flows weather → calendar → markets → politics → world → tech in one continuous natural voice track.

This sprint has the highest polish density in the project. It's not just gluing modules together — it's fighting Gemini's tendency to hallucinate locations and stock prices, engineering news quotas so politics doesn't drown out world coverage (or vice versa), and making the 2-minute audio interruptible so the user can bail with Esc when they've heard enough.

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

### Briefing Orchestrator (4 pts)

*The `modules/briefing.py` synthesizer that pulls all data sources and asks Gemini to weave them into a natural spoken monologue.*

| Task | Estimate |
|------|----------|
| Assemble context: weather + calendar + markets + politics + world + tech into structured dict | 1 hr |
| Draft system prompt with hard rules: no invented locations, no invented tickers, no meta-commentary | 1.5 hr |
| First pass — `--now` triggers full pipeline (`43a4051`) | 1 hr |
| Polish: block hallucinated locations, push past safe closes (`5951d0d`) | 0.5 hr |

**Total: ~4 hrs**

### News Depth (3 pts)

*Guarantee coverage balance across categories so the briefing never becomes all-politics or all-tech.*

| Task | Estimate |
|------|----------|
| Migrate `NEWS_TOPICS` from flat list to structured dicts (label + category + query + domains) | 1 hr |
| Enforce quotas: 2 politics + 1 world + 1 markets + 1 tech per briefing | 1 hr |
| Domain whitelist per category (Reuters, TechCrunch, Ars, Verge, etc.) (`95c11a3`) | 1 hr |

**Total: ~3 hrs**

### Interruptible Playback (3 pts)

*Long-form briefings need a bail button — `speak_interruptible()` listens for Esc via a background thread.*

| Task | Estimate |
|------|----------|
| Add `keyboard` dependency, research Windows admin implications | 0.5 hr |
| Implement `speak_interruptible()` with `threading.Event` interrupt signaling | 1.5 hr |
| Keep original `speak()` unchanged (backward compat for quick responses) | 0.5 hr |
| Graceful fallback if `keyboard` hooks fail without admin (`61983d0`) | 0.5 hr |

**Total: ~3 hrs**

### Caching Layer (2 pts)

*15-minute TTL cache for the briefing plus stale-fallback if regeneration fails (network hiccup, Gemini timeout).*

| Task | Estimate |
|------|----------|
| Serialize briefing (text + MP3 path + timestamp) to `data/briefing_cache.json` | 1 hr |
| Stale-on-failure fallback: if generation fails, serve last-known briefing (`71f7c7e`) | 0.5 hr |
| Wire `--now` flag in `friday.py` to call the cached path (`e7a29c8`) | 0.5 hr |

**Total: ~2 hrs**

### tkinter Dashboard (Deprecated) (2 pts)

*Initial dashboard implementation using Python's built-in tkinter. Deprecated at start of Sprint 5 in favor of Tauri + web frontend when scope expanded to include JARVIS-style visual animations that tkinter fundamentally can't render. Retained in git history at commits `3ea3cec` and `690920c`.*

| Task | Estimate |
|------|----------|
| Skeleton dashboard with placeholder data (`3ea3cec`) | 1.5 hr |
| Live clock + working refresh button (`690920c`) | 1 hr |
| Decision to deprecate — Tauri vs tkinter analysis | 0.5 hr |

**Total: ~3 hrs (deprecated but time invested)**

---

## Initial Task Assignment

Solo project — all tasks assigned to Preetam.

---

## Sprint Retrospective

**Delivered:** Briefing orchestrator (`43a4051`), polish pass (`5951d0d`), caching (`71f7c7e`), `--now` wiring (`e7a29c8`), interruptible playback (`61983d0`), and structured news topics with balanced coverage (`95c11a3`). End-to-end: `python friday.py --now` produces a ~2-minute spoken briefing that can be interrupted by Esc.

**Discoveries:** Gemini's default temperature invents plausible-sounding stock movements when the input data is sparse — had to add explicit "if a value is missing, skip that section entirely" rules. The `keyboard` library needs admin on Windows to register global hooks; fell back to acknowledging failure silently and continuing without interrupt support rather than crashing. News quotas mattered more than expected — before the balance system, tech dominated every briefing because NewsAPI's tech feed is denser.

**Deferred:** UI/visual layer entirely — that's Sprint 5+ territory (Tauri rewrite). Migrating stocks off Alpha Vantage's tight free tier bumped to Sprint 5 as well.

---

## Burnup Chart

Generated at sprint close. See `docs/sprint_reports/sprint4_burnup.png`. *(Chart generation pending — Sprint 7 deliverable.)*
