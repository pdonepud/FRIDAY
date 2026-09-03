# Sprint 7 Plan — FRIDAY

**Product:** FRIDAY (Personal AI Assistant)
**Team:** Solo — Preetam Donepudi
**Sprint completion date:** Planned — June 30, 2026
**Revision:** 1.0 — August 11, 2026

---

## Goal

Transform FRIDAY from "solo hobby project" to "collaboration-ready open source project." Add the testing, CI/CD, PR workflow, and code review infrastructure that professional engineering teams rely on. This sprint is the portfolio-hardening sprint — the artifacts shipped here (test coverage badge, CI green checkmarks, CodeRabbit review comments on merged PRs) are what turn FRIDAY from a demo into an interview-worthy engineering project.

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

### Testing Foundation (5 pts)

*Real test coverage across every module. Portfolio credibility comes from real tests, not just working code.*

| Task | Estimate |
|------|----------|
| Set up pytest with directory structure: `tests/unit/`, `tests/integration/`, `tests/api/` | 1 hr |
| Unit tests for `modules/gemini.py`, `modules/weather.py`, `modules/stocks.py` | 2 hrs |
| Unit tests for `modules/calendar_api.py`, `modules/news.py`, `modules/briefing.py` | 2 hrs |
| Integration tests for FastAPI endpoints in `tests/api/test_api.py` | 1.5 hrs |
| pytest fixtures for OAuth token mocking, API response mocking | 1 hr |
| Coverage report via `pytest-cov`, target 60%+ | 0.5 hr |

**Total: ~8 hrs**

### CI/CD Pipeline (3 pts)

*GitHub Actions runs lint + tests on every push and every PR. Red X on failure blocks merge to main.*

| Task | Estimate |
|------|----------|
| Create `.github/workflows/ci.yml` — matrix over Python 3.11, 3.12 | 1 hr |
| Steps: install deps → ruff lint → pytest → upload coverage | 1 hr |
| Coverage badge added to root `README` | 0.25 hr |
| Optional smoke build of Tauri app on Ubuntu runner | 0.75 hr |

**Total: ~3 hrs**

### PR Workflow + Branch Protection (2 pts)

*Stop pushing to `main` directly. Every change goes through a PR with branch protection.*

| Task | Estimate |
|------|----------|
| Enable branch protection on `main`: require PR + CI green + 1 approval | 0.5 hr |
| Create `.github/pull_request_template.md` with checklist | 0.5 hr |
| Document feature branch naming: `feat/phase-X.Y-description`, `fix/...`, `docs/...` | 0.5 hr |
| Backfill README with a "Contributing" section explaining the workflow | 0.5 hr |

**Total: ~2 hrs**

### AI Code Reviewer — CodeRabbit (1 pt)

*Automated PR review by CodeRabbit — advisory, not merge-blocking.*

| Task | Estimate |
|------|----------|
| Install CodeRabbit GitHub App on the FRIDAY repo (completed in Sprint 5) | 0 hr |
| Configure `.coderabbit.yaml` for review style: language-aware, security-first | 0.75 hr |
| Verify a test PR gets reviewed correctly | 0.25 hr |

**Total: ~1 hr**

### Code Documentation + ADRs (3 pts)

*Every module and function documented. Every major architectural decision captured as an ADR.*

| Task | Estimate |
|------|----------|
| Google-style docstrings for every function in `modules/` and `server/api.py` | 2.5 hrs |
| Module-level docstrings explaining purpose + design constraints | 0.5 hr |
| Create `docs/decisions/` folder with ADRs for: yfinance migration, tkinter→Tauri, Python-plays-audio, FastAPI vs Flask | 2 hrs |
| `CASE_STUDY.md` final polish + remove from `.gitignore` to make public | 0.5 hr |

**Total: ~5 hrs**

**Sprint 7 total: ~19 hrs** (upper end of a solo sprint — this is a big one)

---

## Initial Task Assignment

Solo project — all tasks assigned to Preetam.

---

## Sprint Retrospective

*Not yet started — this sprint is in the backlog.*

---

## Burnup Chart

Will be generated at sprint close.
