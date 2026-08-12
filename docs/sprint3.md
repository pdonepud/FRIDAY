# Sprint 3 Plan — FRIDAY

**Product:** FRIDAY (Personal AI Assistant)
**Team:** Solo — Preetam Donepudi
**Sprint completion date:** March 31, 2026
**Revision:** 1.0 — August 11, 2026

> *This sprint doc was written retrospectively in August 2026 to document work completed during this period. The Scrum-style sprint framing was adopted at the start of Sprint 5 to formalize the project structure going forward. Task estimates are based on actual time recorded in git history.*

---

## Goal

Make FRIDAY personal by giving it access to *your* calendar. Where Sprint 2 pulled generic public data, this sprint introduces OAuth 2.0 so FRIDAY can read the user's Google Calendar — the first module that says "your 3pm meeting" instead of "meetings in general."

The trickier work here is on OAuth ergonomics: the assistant should remember the user across sessions (token refresh), fail gracefully when the token expires, and support multiple calendars per account so shared family/team calendars roll into the daily briefing.

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

### OAuth Flow (4 pts)

*Google OAuth 2.0 with read-only scope, cached tokens, and forced account selection so login is deterministic across machines.*

| Task | Estimate |
|------|----------|
| Create Google Cloud project, enable Calendar API, download `credentials.json` | 0.5 hr |
| Add `credentials.json` and `token.json` to `.gitignore` (private per-user files) | 0.25 hr |
| Build OAuth helper in `modules/calendar_api.py` using `google-auth-oauthlib` | 2 hr |
| Wire token caching to `token.json` and refresh-on-expiry | 1 hr |
| Force `prompt="select_account"` so login is explicit not sticky (`bdcedc6`) | 0.25 hr |

**Total: ~4 hrs**

### Calendar Module (5 pts)

*Multi-calendar event fetching with service caching, plus `--calendar` and `--next` CLI flags.*

| Task | Estimate |
|------|----------|
| `get_today()` — list events from midnight local through midnight local next day | 1.5 hr |
| `get_next_event()` — find the next non-past event across all calendars | 1 hr |
| Multi-calendar support — iterate all subscribed calendars, merge + sort (`9f9fef5`) | 1.5 hr |
| Cache the `build("calendar", "v3", ...)` service handle across calls in a session | 0.5 hr |
| Wire `--calendar` and `--next` flags in `friday.py` (`bf059be`) | 0.5 hr |

**Total: ~5 hrs**

---

## Initial Task Assignment

Solo project — all tasks assigned to Preetam.

---

## Sprint Retrospective

**Delivered:** OAuth helper (`bdcedc6`), multi-calendar fetcher (`9f9fef5`), and both CLI flags wired (`bf059be`). Verified end-to-end against `donepudipreetam2009@gmail.com` with three subscribed calendars.

**Discoveries:** Google's OAuth flow with `prompt="select_account"` is critical for machines with multiple Google accounts — without it the flow silently picks the first cached account, which felt like a bug the first three times. The service handle is expensive to build (~200ms) and worth caching within a single session but *not* worth persisting to disk (token refresh gets weird).

**Deferred:** Write scope (creating events from FRIDAY) — read-only is safer and covers the briefing use case; write comes back in a later sprint if voice-command event creation is ever prioritized.

---

## Burnup Chart

Generated at sprint close. See `docs/sprint_reports/sprint3_burnup.png`. *(Chart generation pending — Sprint 7 deliverable.)*
