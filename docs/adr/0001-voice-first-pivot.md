# ADR 0001 — Voice-first pivot

**Status:** Accepted (retroactive documentation)
**Date:** 2026-08 (decision); 2026-09 (documented)

## Context

FRIDAY was originally scoped as a JARVIS-inspired always-on desktop assistant with a visual HUD as the primary interface. Sprints 1–6 built toward that: a Tauri + FastAPI application with a concentric-arc dashboard displaying live watchlist prices, weather forecasts, news headlines, and system status. The stack was Python (FastAPI backend, data-source integrations) plus TypeScript/Tauri (frontend HUD panels). By end of Sprint 6, five of the planned six panels had shipped (#15 watchlist, #16 news, #17 hourly weather, #19 server auto-start), and Sprint 7 ("Professionalization") was scoped to add pytest, CI, branch protection, CodeRabbit config, and docstrings + ADRs.

Two forces produced the pivot before Sprint 7 executed:

1. **The HUD wasn't the interaction I actually wanted.** Static panels displaying pre-fetched data are lower-value than a conversational assistant I can ask arbitrary questions and delegate work to. The HUD's ceiling is "always-on dashboard I glance at"; the conversational ceiling is "assistant I hand real tasks to." The latter is closer to what "JARVIS" means as an aspiration.

2. **Emerging tooling changed what was cheap to build.** Robust STT (Deepgram), high-quality TTS (ElevenLabs), tool-use in frontier LLMs, and MCP as an emerging standard for external integrations meant a voice-first agent that could actually *do* things — not just show them — became buildable by a single developer in a reasonable timeframe.

Continuing with the HUD would have meant investing further in an interface that no longer matched the target experience, then either abandoning it later or maintaining two products.

## Decision

Pivot FRIDAY from HUD-first to voice-first. The dashboard direction is discontinued. FRIDAY's architecture is redefined as a Python-only conversational agent with:

- A single source-of-truth spec document (`AGENT.md`) covering personality, constraints, tier plan, and thin-seam principles for future provider swaps
- A six-tier build plan replacing the sprint model: baseline conversation → first tools → voice I/O → memory & context → proactive behavior → confirmation gate
- Voice as the primary modality (Tier 3+), with text as a permanent fallback
- Prior HUD code (`ui/`, `server/`, `modules/`) frozen in place but no longer actively developed

Tier boundaries are sequential but not time-boxed. Each tier ships a bounded, verified increment before the next opens.

## Consequences

**Positive:**
- The product ceiling raised significantly — FRIDAY can now execute tasks, not just display data
- Architecture is simpler: one language, one runtime, no cross-process communication between backend and frontend
- The tier plan makes acceptance criteria explicit at each step, replacing the looser "sprint complete when its issues close" model
- AGENT.md as source-of-truth prevents scope drift; every implementation decision references it

**Negative:**
- The HUD work (Sprints 1–6) is now historical — the code stays committed for reference but represents effort that doesn't ship to the new direction
- Several planned Sprint 6 refinements closed as superseded (#3 responsive layout, #27 weather-location display, #28 editable watchlist, #31 shell:allow-open scoping, #32 withGlobalTauri removal)
- Sprint 7 (Professionalization) was redistributed across tier milestones rather than shipped as a single unit
- Sprint 8 (hotkeys + focus mode) was never formalized; hotkeys subsumed into Tier 3 push-to-talk, focus mode dropped

**Neutral:**
- Professionalization work (pytest, CI, docstrings, ADRs) is more valuable in the voice-first direction, not less — a conversational agent with tools needs test coverage and CI more than a passive HUD did
- CodeRabbit and branch protection carry over unchanged

## References

- `AGENT.md` — current source of truth for FRIDAY's design
- Closed issues #3, #18, #27, #28, #31, #32 — HUD-era scope superseded by this pivot
- Closed milestones: Sprint 6 (partially complete), Sprint 7 (scope redistributed)
- PR #35 — first PR under the new direction (Tier 1 baseline)
