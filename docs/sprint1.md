# Sprint 1 Plan — FRIDAY

**Product:** FRIDAY (Personal AI Assistant)
**Team:** Solo — Preetam Donepudi
**Sprint completion date:** January 31, 2026
**Revision:** 1.0 — August 11, 2026

> *This sprint doc was written retrospectively in August 2026 to document work completed during this period. The Scrum-style sprint framing was adopted at the start of Sprint 5 to formalize the project structure going forward. Task estimates are based on actual time recorded in git history.*

---

## Goal

Stand up the FRIDAY project skeleton and prove the assistant can speak. This sprint focuses on the boring-but-critical foundation: repo hygiene, a two-file config pattern (`config.py` gitignored + `config.example.py` committed) so future contributors have a template while the risk of accidentally committing secrets is reduced, and a working text-to-speech pipeline that greets the user out loud.

The point of shipping voice first is that FRIDAY is a voice-first product — everything downstream (briefings, Q&A, ambient nudges) leans on this layer, so it needed to work end-to-end before adding intelligence on top.

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

### Setup & Config (3 pts)

*Repo hygiene, dependency management, and the two-file config pattern that reduces the risk of accidentally committing secrets. (Key-rotation procedure for a leaked credential is planned for Sprint 7's security notes; the dual-config pattern is risk-reduction, not a guarantee.)*

| Task | Estimate |
|------|----------|
| `git init`, first commit (`3186e6c`), add `README.md` (`7fda5a8`) | 0.5 hr |
| Author `.gitignore` covering `config.py`, `token.json`, `credentials.json`, `__pycache__/` | 0.5 hr |
| Write `requirements.txt` with initial pins (`edge-tts`, `pygame`) | 0.5 hr |
| Design dual-file config pattern: `config.py` (secrets, gitignored) + `config.example.py` (template, committed) | 1.5 hr |

**Total: ~3 hrs**

### Voice Layer (6 pts)

*A reusable TTS wrapper and a working greeting so FRIDAY has a voice from day one.*

| Task | Estimate |
|------|----------|
| Research `edge-tts` vs paid alternatives; pick free Microsoft neural voices | 1 hr |
| Build `modules/voice.py` — `speak(text)` wrapper generating MP3 via `edge-tts` | 2 hr |
| Wire `pygame.mixer` for playback with cleanup of temp files | 1.5 hr |
| Author `modules/greeting.py` — time-of-day-aware greeting string | 1 hr |
| End-to-end smoke test: `python friday.py` speaks a greeting (`bf3b586`) | 0.5 hr |

**Total: ~6 hrs**

---

## Initial Task Assignment

Solo project — all tasks assigned to Preetam.

---

## Sprint Retrospective

**Delivered:** Project skeleton with voice greeting playing end-to-end. Commits `3186e6c` (init), `7fda5a8` (README), and `bf3b586` (Phase 1: project skeleton + voice greeting) close out the sprint. Dual-config pattern established and honored by every subsequent module.

**Discoveries:** `edge-tts` quality genuinely competes with paid TTS at $0/month — the decision to skip Azure/ElevenLabs saved the project's viability as a free-tier build. `pygame.mixer` requires explicit `pygame.mixer.quit()` to release the MP3 file on Windows or the temp file cleanup fails silently.

**Deferred:** No interruption support yet (Esc to stop) — pushed to Sprint 4 once briefings were long enough to need it.

---

## Burnup Chart

Generated at sprint close. See `docs/sprint_reports/sprint1_burnup.png`. *(Chart generation pending — Sprint 7 deliverable.)*
