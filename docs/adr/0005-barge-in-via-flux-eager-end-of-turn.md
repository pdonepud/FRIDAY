# ADR 0005 — Barge-in via Flux EagerEndOfTurn

**Status:** Proposed
**Date:** 2026-10-05
**Supersedes / refines:** refines ADR-0003 (voice pipeline)

## Context

Tier 3 shipped a push-to-talk (PTT) voice loop: hold Right Alt, speak, release, hear Lily reply. STT finalization is forced on PTT release via Deepgram Flux's `ForceEndTurn` event. This design is correct for first-cut voice I/O but has a known UX ceiling — the user cannot interrupt FRIDAY mid-reply. The TTS stream plays to completion. Natural conversation requires barge-in.

ADR-0003 pre-committed us to the Flux `EagerEndOfTurn` / `TurnResumed` pattern as the barge-in path. This ADR locks the design.

Flux emits two events relevant here:

- `EagerEndOfTurn` — a speculative turn finalization fired while audio may still be arriving. Historically interpreted as "the user probably just stopped talking."
- `TurnResumed` — fired when the user keeps speaking after an eager event, invalidating the speculative finalization.

The question is not whether to migrate, but how the migration composes with the Tier 3 PTT gate, the ElevenLabs streaming TTS, the `AudioPlaybackError` + watchdog work from #55, and the mid-stream LLM cancellation work also from #55.

## Decision

**1. PTT frames turns that originate from IDLE; during SPEAKING, the mic stays open.**

PTT is the explicit "I am starting a new turn" signal when FRIDAY is not already talking: PRESS opens the window, RELEASE closes it, and STT runs continuously inside that window. In this flow, `EagerEndOfTurn` finalizes the turn speculatively and a subsequent `TurnResumed` retracts the finalization and keeps the turn open. This replaces the current `ForceEndTurn`-on-release behavior for IDLE-origin turns.

During SPEAKING, the mic stays open without PTT. Barge-in is not a new PTT press — it is the user speaking over Lily. PTT is NOT required to interrupt playback. The signal that triggers barge-in is covered in Decision 2.

**2. During SPEAKING, `StartOfTurn` is the barge-in trigger — confirmed before cancellation.**

This aligns with ADR-0003, which reserves `StartOfTurn` for the barge-in interrupt and `EagerEndOfTurn` for speculative LLM execution. The sequence is strictly:

1. Flux emits `StartOfTurn` while the state machine is in SPEAKING.
2. FRIDAY opens a short confirmation window (initial value: 150 ms, tunable via `_BARGEIN_CONFIRM_MS`). Playback continues uninterrupted during this window.
3. If sustained speech is observed within the window, FRIDAY cancels TTS playback and transitions out of SPEAKING. The cancel path is the destructive cancel from Decision 3 — the ElevenLabs stream drains, PortAudio closes via the nested try/finally pattern from #55, and SPEAKING ends.
4. If no sustained speech is observed, the `StartOfTurn` is treated as a false trigger (throat clear, Lily's own voice bleeding through, brief room noise). No state transition occurs. No cancellation runs. Playback has already been playing throughout, so nothing resumes — nothing was paused.

This ordering means the user experiences a short lag (up to `_BARGEIN_CONFIRM_MS` ms) between starting to speak and Lily stopping. This is accepted for v1 (see Negative Consequences). The alternative — pausing playback immediately on `StartOfTurn` and resuming on rejection — would require a non-destructive "paused" state in the playback pump that does not exist in the Tier 3 contract and is explicitly deferred to a follow-up ADR.

`EagerEndOfTurn` and `TurnResumed` continue to serve their Decision 1 role (speculative finalization and retraction during PTT-framed turns). They are not barge-in triggers.

**3. Playback cancellation is cooperative, not forced.**

A new `BargeInEvent` (or a shared `asyncio.Event` — chosen at implementation time) signals the playback pump. The pump drains the ElevenLabs stream, closes the PortAudio stream through the nested `try`/`finally` pattern established in #55, and transitions out of the SPEAKING state. The `AudioPlaybackError`/watchdog plumbing from #55 covers the hard-failure path; barge-in is the soft-cancel path.

**4. A single state machine owns voice-turn transitions.**

The current `voice_loop()` carries control flow in-line. Barge-in adds enough edges that a formal state machine is necessary:

Only the state owner mutates the state. Everything else observes.

**5. Mid-stream LLM cancellation is now a real path.**

The `httpx` cancel semantics exercised in #55 cover this. When BARGE_IN fires during THINKING or SPEAKING, the in-flight Anthropic stream is cancelled via `task.cancel()`; the `CancelledError` is caught at the LLM seam and surfaced as a clean turn abort.

## Consequences

**Positive:**

- Natural conversation becomes possible: the user can cut in without waiting out a long reply.
- The state machine makes future voice features (filler audio, "one moment" during tool calls, confirmation prompts in Tier 7) tractable instead of ad-hoc.
- Reuses the error plumbing from #55 rather than inventing parallel machinery.

**Negative:**

- STT is now double-duplex with TTS. On devices without headphones or hardware echo cancellation, Lily's own voice may trigger `StartOfTurn` and cause spurious barge-in. The 150 ms confirmation window from Decision 2 is the first-line mitigation; the README must additionally recommend headphones. Deeper mitigation (output-aware STT gating or software AEC) is explicitly deferred.
- Up to 150 ms of perceived lag between the user starting to speak and Lily stopping. This is a direct consequence of the confirm-before-cancel ordering in Decision 2. Reducing the confirmation window reduces the lag but increases the false-positive rate; the right tradeoff is unknown until we use it. If tuning the window reveals that the lag is unacceptable at any defensible false-positive rate, a follow-up ADR will introduce a non-destructive "paused" playback state so cancellation can be delayed until confirmation without the user hearing continued speech. This is deliberately not v1.
- The voice-turn state machine is new surface area; test coverage must be strict (legal transitions + rejected illegal transitions).
- Partial ElevenLabs generations still bill when playback is cancelled. Cost is bounded by typical reply length and user interruption rate; acceptable.
- Barge-in grace period (300 ms) is a knob. Too short → false interruptions on breath pauses. Too long → feels unresponsive. Tuning expected after real-world use.

**Neutral:**

- No new third-party dependencies.
- No change to the Tier 3 LLM seam contract (`agent/claude.py` interface).

## Alternatives Considered

### A. Status quo (PTT-only, no barge-in)

Simplest. Rejected — explicit project goal is natural conversation and companion-level responsiveness. Blocks Tier 4 experience even if memory lands.

### B. Full always-on STT (no PTT gate)

Removes PTT entirely. Uses VAD (voice activity detection) to open and close turns. Rejected for now:

- Introduces hot-mic privacy posture; room noise and bystander speech become inputs.
- Requires solid VAD, which Deepgram Flux handles but still needs tuning and false-positive management.
- Loses the explicit intent signal that PTT provides — Lily can tell "user is addressing me" from "user is talking to someone else in the room."

PTT-with-barge-in is a strict improvement over status quo without taking on these costs. A future ADR may revisit always-on once the companion model justifies it.

### C. Double-tap PTT to interrupt

Keeps the Tier 3 design but adds a second gesture. Rejected — feels unnatural, defeats the point (natural conversation shouldn't require learning a gesture), and does nothing for the user who starts speaking without pressing anything.

### D. PTT required for every utterance, including barge-in

Would make PTT the gate for every turn, requiring the user to press PTT a second time to interrupt during playback. Rejected — users interrupt by speaking, not by pressing. Matching user expectation beats preserving a uniform PTT model, and the SPEAKING-state mic-open window from Decision 1 handles the hot-mic concern because playback already masks ambient speech from most false-trigger conditions.

## Implementation Notes

- Tier 4a issues #70, #71, #72 track implementation.
- #70 handles STT-side event migration.
- #71 handles playback cancel wiring.
- #72 handles the state machine refactor and must land last so it absorbs both.
- Barge-in confirmation window (150 ms) belongs in `agent/stt.py` as a module-level `_BARGEIN_CONFIRM_MS` with a `TODO(#70-tuning)` comment. `EagerEndOfTurn` grace behavior for turn finalization has its own tunable, out of scope for this ADR.
- README section on headphone recommendation added as part of #72 or a follow-up docs issue.

## Open Questions (Not Blocking)

- Should FRIDAY speak a short acknowledgment when barge-in fires ("oh, sorry") or just stop? Current decision: just stop. Revisit after use.
- Does barge-in during THINKING (before any audio has played) warrant different treatment than barge-in during SPEAKING? Current decision: same path. Revisit if the UX feels off.
- If the 150 ms (or tuned-lower) confirm-before-cancel lag is perceptibly bad during #70 implementation, follow up with an ADR introducing a pause-and-resume playback state so the destructive cancel can be delayed until barge-in is confirmed. Track as a tuning issue separate from #70/#71.

## References

- Deepgram Flux docs — EagerEndOfTurn and TurnResumed events: <https://developers.deepgram.com/docs/flux>
- PR #55 — Graceful error handling for voice pipeline (introduces `AudioPlaybackError`, playback dual-watchdog, nested-finally stream teardown, and `httpx` mid-stream cancellation path that this ADR builds on).
- ADR-0003 — Voice architecture (the pre-commitment this ADR cashes in).
- ADR-0003 §Tier-4 migration path for STT — defines the Flux event vocabulary (StartOfTurn / EagerEndOfTurn / TurnResumed / EndOfTurn) and reserves StartOfTurn for the barge-in interrupt.
