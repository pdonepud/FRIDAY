# Sprint 2 Plan — FRIDAY

**Product:** FRIDAY (Personal AI Assistant)
**Team:** Solo — Preetam Donepudi
**Sprint completion date:** February 28, 2026
**Revision:** 1.0 — August 11, 2026

> *This sprint doc was written retrospectively in August 2026 to document work completed during this period. The Scrum-style sprint framing was adopted at the start of Sprint 5 to formalize the project structure going forward. Task estimates are based on actual time recorded in git history.*

---

## Goal

Give FRIDAY a brain and external senses. This sprint wires in Gemini as the LLM backend (behind a personality system prompt), then layers three data sources so the assistant has something factual to talk about: weather from Open-Meteo, stocks from Alpha Vantage, and news from NewsAPI.

Every module lands with a caching layer and a standalone `__main__` block so it can be tested in isolation — the modularity discipline that later made Sprint 4's briefing orchestrator trivial to assemble.

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

### Gemini Integration (5 pts)

*REST wrapper around the Gemini API with a personality system prompt and a `--ask` CLI flag for on-demand Q&A.*

| Task | Estimate |
|------|----------|
| Sign up for Gemini API key, add `GEMINI_API_KEY` to `config.py` + example | 0.5 hr |
| Build `modules/gemini.py` REST wrapper (chose `requests` over official SDK to keep deps minimal) | 2 hr |
| Draft personality system prompt (dry, concise, JARVIS-adjacent tone) | 1 hr |
| Wire `--ask "question"` flag in `friday.py` entry point | 1 hr |
| Manual test: several Q&A rounds, verify tone consistency (`37f0627`) | 0.5 hr |

**Total: ~5 hrs**

### Data Modules (8 pts)

*Three self-contained data fetchers — each with caching, error handling, and a standalone `__main__` block.*

| Task | Estimate |
|------|----------|
| `modules/weather.py` — fetch current + 2-day forecast from Open-Meteo (no API key) with a 15-minute cache TTL, Santa Cruz coords hard-coded | 1.5 hr |
| Weather cache lookup logic: skip external call if cached value is <15 min old; fall back to stale cache on Open-Meteo failure | 0.5 hr |
| `modules/stocks.py` — Alpha Vantage TIME_SERIES_INTRADAY, per-symbol cache, rate-limit-aware (`bdb032b`) | 2.5 hr |
| Sign up NewsAPI key, add `NEWS_API_KEY` config | 0.5 hr |
| `modules/news.py` — top-headlines endpoint, 30-min cache, title/description dedup (`5eee0b9`) | 2.5 hr |
| Standalone test harness for each module (`if __name__ == "__main__"`) | 1 hr |

**Total: ~8.5 hrs**

---

## Initial Task Assignment

Solo project — all tasks assigned to Preetam.

---

## Sprint Retrospective

**Delivered:** Gemini-backed Q&A live via `--ask` (`37f0627`), and three data modules — weather + stocks (`bdb032b`) and news (`5eee0b9`) — all cached and independently testable.

**Discoveries:** Alpha Vantage's 25 requests/day free tier is stricter than the docs suggest; the per-symbol cache barely covered it. Flagged the migration to `yfinance` for a later sprint (eventually shipped in Sprint 5, commit `48c69e`). Open-Meteo is a hidden gem — no key, no throttling, accurate enough for a personal briefing. NewsAPI's dedup by title similarity catches ~30% of duplicates cross-source.

**Deferred:** News source-quality filtering (domain whitelisting) pushed to Sprint 4 where the briefing needed mainstream-journalism-only coverage.

---

## Burnup Chart

Generated at sprint close. See `docs/sprint_reports/sprint2_burnup.png`. *(Chart generation pending — Sprint 7 deliverable.)*
