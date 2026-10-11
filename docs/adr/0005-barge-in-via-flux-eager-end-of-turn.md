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

*Prerequisite: Flux session configuration.* This decision depends on Flux emitting both `EagerEndOfTurn` (speculative) and natural `EndOfTurn` events at a cadence that matches real conversation. Two thresholds govern this, and the current configuration does not match what Decision 1 assumes:

- `eot_threshold` is currently set to `1.0` in FRIDAY's Flux session setup (per ADR-0003's Tier 3 choice to suppress native turn detection in favor of `ForceEndTurn` on PTT release). Flux's own default is approximately `0.7`. Decision 1 requires lowering this toward Flux's default — or lower — so natural `EndOfTurn` fires on conversational-length pauses inside the `_PTT_RELEASE_GRACE_MS` window.
- `eager_eot_threshold` is unset in the current configuration, which disables `EagerEndOfTurn` events entirely but does NOT suppress ordinary `EndOfTurn`. Decision 1 requires setting it to a value below the chosen `eot_threshold` to enable speculative finalization and the `TurnResumed` retraction path.

Flux also requires `eager_eot_threshold <= eot_threshold`; a higher eager value is a configuration error at session open. #70 is responsible for picking a compatible pair, wiring both into the session setup, and adding a startup assertion so the pairing cannot silently regress (see Implementation Notes).

PTT is the explicit "I am starting a new turn" signal when FRIDAY is not already talking: PRESS opens the window, RELEASE closes it, and STT runs continuously inside that window. In this flow, `EagerEndOfTurn` finalizes the turn speculatively and a subsequent `TurnResumed` retracts the finalization and keeps the turn open.

On RELEASE, FRIDAY stops forwarding new audio to Flux but keeps the receive path open for a bounded grace period (`_PTT_RELEASE_GRACE_MS`, default 500 ms, tunable). The grace lets Flux's natural turn-finalization events (`EagerEndOfTurn` → `EndOfTurn`, or an already-in-flight `TurnResumed` → `EndOfTurn`) complete without being cut short. If `EndOfTurn` arrives within the grace, the turn finalizes normally and no `ForceEndTurn` is sent. If the grace expires without `EndOfTurn`, FRIDAY sends `ForceEndTurn` as a bounded liveness fallback to prevent the receive path and `transcribe()`'s `done_future` from hanging indefinitely. Total receive wait from RELEASE is therefore bounded by `_PTT_RELEASE_GRACE_MS` plus Flux's `ForceEndTurn` ack time.

This differs from the current Tier 3 `ForceEndTurn`-on-release behavior, which fires immediately regardless of in-flight events. The new flow gives Flux a chance to close naturally — preserving the accuracy of speculative finalization and respecting `TurnResumed` retractions — while retaining `ForceEndTurn` strictly as a bounded liveness guarantee.

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

**5. Mid-stream LLM cancellation on barge-in preserves the pending turn.**

Barge-in during SPEAKING cancels the in-flight LLM stream. This cancellation must be distinguishable from error-caused cancellation (network failure, provider drop — the #55 flow) so that `voice_loop` does not treat barge-in as a failed turn and drop the user's input.

The pattern:

1. The LLM stream (`stream_sentences()`) and its downstream TTS feed run in a dedicated subtask spawned by `voice_loop`, not inline in the voice_loop task itself. `voice_loop` supervises the subtask and owns state transitions.
2. When `BARGE_IN` fires, only the stream subtask is cancelled via `task.cancel()`. The `voice_loop` task continues running — it awaits the subtask's cleanup and drives the transition to LISTENING.
3. The subtask's `CancelledError` is caught at the LLM seam and surfaced as a clean turn abort. The `httpx` cancel semantics exercised in #55 cover the actual stream close.
4. `voice_loop` distinguishes barge-in abort from error abort by the originating state (BARGE_IN), not by `task.uncancel()` return values. On barge-in abort, `voice_loop` does NOT remove the pending user message and does NOT drop the barge-in-triggering utterance — that utterance becomes the input for the next turn.
5. Error-caused cancellation (LLM-side network drop or provider failure during `stream_sentences`) continues to follow the #55 pattern unchanged. Per `agent/loop.py`'s existing error branch (`except (APIConnectionError, NetworkError):`): FRIDAY prints `[net] Lost connection mid-response — try again.` to the console, removes the pending user message, and returns to LISTENING. No voice apology is played on LLM-side errors. The `[friday] Sorry, I lost my voice. Try again.` line that fires on TTS-side errors in #55 is itself a plain stdout print, not a TTS synthesis, and is scoped to the `except tts.TTSError` branch only — it is not the LLM-error path. Whether LLM-side errors should also get a voice apology is a separate design question deferred to a follow-up (see Open Questions).

Barge-in applies to the SPEAKING state only in v1, matching Decision 4's state machine (`SPEAKING ↔ BARGE_IN → LISTENING`). During THINKING, no audio is playing, so there is nothing to barge in on; a user who wants to cancel a pending response during THINKING is performing a different interaction, deferred to a follow-up (see Open Questions).

## Consequences

**Positive:**

- Natural conversation becomes possible: the user can cut in without waiting out a long reply.
- The state machine makes future voice features (filler audio, "one moment" during tool calls, confirmation prompts in Tier 7) tractable instead of ad-hoc.
- Reuses the error plumbing from #55 rather than inventing parallel machinery.

**Negative:**

- STT is now double-duplex with TTS. On devices without headphones or hardware echo cancellation, Lily's own voice may trigger `StartOfTurn` and cause spurious barge-in. The 150 ms confirmation window from Decision 2 is the first-line mitigation; the README must additionally recommend headphones. Deeper mitigation (output-aware STT gating or software AEC) is explicitly deferred.
- Up to 150 ms of perceived lag between the user starting to speak and Lily stopping. This is a direct consequence of the confirm-before-cancel ordering in Decision 2. Reducing the confirmation window reduces the lag but increases the false-positive rate; the right tradeoff is unknown until we use it. If tuning the window reveals that the lag is unacceptable at any defensible false-positive rate, a follow-up ADR will introduce a non-destructive "paused" playback state so cancellation can be delayed until confirmation without the user hearing continued speech. This is deliberately not v1.
- Decision 1's RELEASE-grace flow is coupled to Flux session configuration (`eot_threshold`, `eager_eot_threshold`) that must be set correctly at session open. Degradation modes:
    - If `eager_eot_threshold` is unset, `EagerEndOfTurn` events do not arrive. Speculative finalization and the `TurnResumed` retraction path do not function; ordinary `EndOfTurn` still fires and can land inside the grace, so the receive wait is not unbounded, but the turn-accuracy benefits of eager mode are lost.
    - If `eot_threshold` remains at FRIDAY's current `1.0` (per ADR-0003's Tier 3 choice), Flux suppresses confidence-based `EndOfTurn` detection entirely — the confidence threshold is never reached. Flux's separate `eot_timeout_ms` safety net (currently `30,000 ms` in FRIDAY's session setup, per `agent/stt.py:86 EOT_TIMEOUT_MS = 30000`) fires `EndOfTurn` after that timeout regardless of confidence, but 30 seconds is well outside any plausible `_PTT_RELEASE_GRACE_MS`. Under this configuration, `ForceEndTurn` is not the "de-facto path" but the ONLY path — the bounded liveness fallback becomes the primary mechanism. #70 must lower `eot_threshold` (toward Flux's ~0.7 default) to re-enable confidence-based detection inside the grace, and may also want to lower `eot_timeout_ms` for the pathological case where Flux never gains confidence (prolonged background noise, etc.).
    - If `eager_eot_threshold > eot_threshold`, Flux rejects the session configuration at open. This is a hard error, not a silent degradation.
  These are configuration concerns, not architectural ones — tuning lives in #70 — but a future Flux SDK default change could silently alter behavior and warrants a session-setup assertion in `agent/stt.py` that fails loudly if either threshold is missing or the ordering constraint is violated.
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

Would make PTT the gate for every turn, requiring the user to press PTT a second time to interrupt during playback. Rejected — users interrupt by speaking, not by pressing. Matching user expectation beats preserving a uniform PTT model. The hot-mic concern raised by Decision 1's SPEAKING-state mic-open window is real and is tracked in Negative Consequences (echo-cancellation false triggers on Lily's own voice); the 150 ms confirmation window from Decision 2 and the headphone recommendation together mitigate it. Playback does NOT mask ambient speech from Flux's input, since the mic stays open throughout SPEAKING.

## Implementation Notes

- Tier 4a issues #70, #71, #72 track implementation.
- #70 handles STT-side event migration.
- #71 handles playback cancel wiring.
- #72 handles the state machine refactor and must land last so it absorbs both.
- Barge-in confirmation window (150 ms) belongs in `agent/stt.py` as a module-level `_BARGEIN_CONFIRM_MS` with a `TODO(#70-tuning)` comment.
- PTT release grace (500 ms) belongs in `agent/stt.py` as a module-level `_PTT_RELEASE_GRACE_MS` with a `TODO(#70-tuning)` comment. Governs the bounded wait for Flux to emit `EndOfTurn` naturally before `ForceEndTurn` fires as a liveness fallback (Decision 1).
- Flux session configuration (per Decision 1 Prerequisite): both `eot_threshold` and `eager_eot_threshold` must be explicitly set when opening the Flux session in `agent/stt.py`. Current state (per ADR-0003 and `deepgram-sdk==7.8.1` in `pyproject.toml`): `eot_threshold = 1.0`, `eager_eot_threshold` unset. Starting values are a tuning task inside #70 — expected to land somewhere below Flux's default `eot_threshold` of ~0.7 (to make natural `EndOfTurn` fire on conversational pauses rather than long silences) and with `eager_eot_threshold` set low enough to produce useful speculative events without excessive `TurnResumed` churn.
- Flux constraint `eager_eot_threshold <= eot_threshold` must hold; a higher eager value is a session-open configuration error. #70 includes a startup assertion in `agent/stt.py` that both values are set and the ordering holds, so the pairing cannot silently regress through a future SDK default change. Both threshold values are TODO(#70-tuning). Reference Deepgram's Flux eager-mode guidance for the ranges that behave sensibly together.
- Flux's EOT safety net (`eot_timeout_ms`, currently `30,000 ms` per `agent/stt.py:86 EOT_TIMEOUT_MS = 30000`) is distinct from `eot_threshold`. `eot_threshold` governs confidence-based detection; `eot_timeout_ms` is a hard fallback that fires `EndOfTurn` after a fixed silence duration regardless of confidence. #70 should consider whether to lower `eot_timeout_ms` alongside `eot_threshold` so the safety net is useful inside RELEASE grace in pathological cases (e.g., prolonged background noise preventing confidence from clearing the threshold). Value is TODO(#70-tuning).
- The LLM stream subtask pattern (Decision 5) is implemented in `agent/loop.py::voice_loop()`: `stream_sentences()` + TTS feed run under an `asyncio.create_task()` whose lifetime is scoped to the SPEAKING state. `voice_loop` holds the task handle and is the only caller of `task.cancel()` on it; barge-in triggers that cancel, nothing else does.
- README section on headphone recommendation added as part of #72 or a follow-up docs issue.

## Open Questions (Not Blocking)

- Should FRIDAY speak a short acknowledgment when barge-in fires ("oh, sorry") or just stop? Current decision: just stop. Revisit after use.
- Does barge-in during THINKING (before any audio has played) warrant different treatment than barge-in during SPEAKING? Current decision: same path. Revisit if the UX feels off.
- If the 150 ms (or tuned-lower) confirm-before-cancel lag is perceptibly bad during #70 implementation, follow up with an ADR introducing a pause-and-resume playback state so the destructive cancel can be delayed until barge-in is confirmed. Track as a tuning issue separate from #70/#71.
- PTT-during-THINKING cancellation: if the user presses PTT while FRIDAY is in THINKING (waiting for the LLM to respond), they likely want to cancel the pending turn and say something else. Not supported in v1 — Decision 5 scopes barge-in to SPEAKING only. If this gap is felt during real use, address with a follow-up that either (a) adds a THINKING-origin cancel path distinct from barge-in, or (b) extends the state machine to allow BARGE_IN from THINKING. File a tuning issue if observed.
- Should LLM-side network errors get a voice apology similar to the TTS-side apology from #55 ("Sorry, I lost my voice")? Current ADR follows the existing agent/loop.py behavior (console message, no TTS). If voice feedback for all failure classes is a UX goal, add it in a focused follow-up; the plumbing (apology-TTS path) already exists on the TTS side and can be extended to the LLM-error branch.

## References

- Deepgram Flux docs — EagerEndOfTurn and TurnResumed events: <https://developers.deepgram.com/docs/flux>
- PR #55 — Graceful error handling for voice pipeline (introduces `AudioPlaybackError`, playback dual-watchdog, nested-finally stream teardown, and `httpx` mid-stream cancellation path that this ADR builds on).
- ADR-0003 — Voice architecture (the pre-commitment this ADR cashes in).
- ADR-0003 §Tier-4 migration path for STT — defines the Flux event vocabulary (StartOfTurn / EagerEndOfTurn / TurnResumed / EndOfTurn) and reserves StartOfTurn for the barge-in interrupt.
