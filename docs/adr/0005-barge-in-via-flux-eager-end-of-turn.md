# ADR 0005 — Barge-in via Flux StartOfTurn (with speculative EagerEndOfTurn / TurnResumed)

**Status:** Accepted
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

*Prerequisite: Flux session configuration.* This decision depends on Flux emitting both `EagerEndOfTurn` (speculative) and natural `EndOfTurn` events at a cadence that matches real conversation. Two thresholds govern this; both landed in #70 (PR #84) at the values `agent/stt.py:101-106` currently holds:

- `EOT_THRESHOLD = 0.7` — lowered from ADR-0003's Tier 3 value of `1.0` toward Flux's natural default so confidence-based `EndOfTurn` fires on conversational-length pauses inside the `PTT_RELEASE_GRACE_MS` window.
- `EAGER_EOT_THRESHOLD = 0.5` — set below `EOT_THRESHOLD` to enable `EagerEndOfTurn` events and the `TurnResumed` retraction path.

Flux also requires `eager_eot_threshold <= eot_threshold`; a higher eager value is a configuration error at session open. The ordering invariant is enforced by an import-time check in `agent/stt.py:138-141` so a future edit that violates it fails loudly in development rather than silently regressing. (The check is an `assert`, which `python -O` would strip; FRIDAY is not run under `-O`.)

PTT is the explicit "I am starting a new turn" signal when FRIDAY is not already talking: PRESS opens the window, RELEASE closes it, and STT runs continuously inside that window. In this flow, `EagerEndOfTurn` finalizes the turn speculatively and a subsequent `TurnResumed` retracts the finalization and keeps the turn open.

On RELEASE, FRIDAY stops forwarding new audio to Flux but keeps the receive path open for a bounded grace period (`PTT_RELEASE_GRACE_MS`, default 500 ms, tunable). The grace lets Flux's natural turn-finalization events (`EagerEndOfTurn` → `EndOfTurn`, or an already-in-flight `TurnResumed` → `EndOfTurn`) complete without being cut short. If `EndOfTurn` arrives within the grace, the turn finalizes normally and no `ForceEndTurn` is sent. If the grace expires without `EndOfTurn`, FRIDAY sends `ForceEndTurn` as a bounded liveness fallback to prevent the receive path and `transcribe()`'s `done_future` from hanging indefinitely. Total receive wait from RELEASE is bounded by `PTT_RELEASE_GRACE_MS` plus Flux's `ForceEndTurn` ack time in the normal case, and by the client-side watchdog at `agent/stt.py:407-414` (`RECEIVE_TIMEOUT_S = 35` seconds, armed when `force_end_sent` fires at `agent/stt.py:343` — immediately after the local send completes, not after Flux acknowledges) if Flux sends no terminal event at all.

This differs from the current Tier 3 `ForceEndTurn`-on-release behavior, which fires immediately regardless of in-flight events. The new flow gives Flux a chance to close naturally — preserving the accuracy of speculative finalization and respecting `TurnResumed` retractions — while retaining `ForceEndTurn` strictly as a bounded liveness guarantee.

During SPEAKING, the mic stays open without PTT. Barge-in is not a new PTT press — it is the user speaking over Lily. PTT is NOT required to interrupt playback. The signal that triggers barge-in is covered in Decision 2.

*Flux's per-turn transcript model.* Flux emits one `EndOfTurn` per detected turn and resets its transcript buffer afterwards. During active PTT hold, a mid-utterance silence can be long enough for Flux to speculatively finalize with a native (model-triggered) `EndOfTurn`. Without special handling, each such event would be a fresh transcript boundary, and speech captured before the ignored boundary would not appear in the terminal `EndOfTurn`'s transcript. FRIDAY accumulates transcripts from ignored model-triggered EndOfTurns during active PTT and joins them (space-separated) with the terminal EndOfTurn's transcript when the turn finalizes (see `agent/stt.py` `_receive`: accumulator at line 422, append point at lines 448-449, join point at lines 466-476).

*`trigger="model"` vs `trigger="manual"` discrimination.* Flux's `EndOfTurn.trigger` field (per Deepgram's Flux reference) takes three values: `model` for native detection fired on confidence threshold, `manual` for the server's response to our `ForceEndTurn`, and `timeout` for `eot_timeout_ms` elapsing. FRIDAY's dispatch (`agent/stt.py:435-485`): `manual` is always terminal. `model` is terminal only when `ptt_release.is_set()` — i.e., we are inside the release-grace window; a `model` EOT received while PTT is still held is ignored (its transcript accumulated per the previous paragraph, log at DEBUG) and `_receive` keeps listening. `timeout` is terminal via `STTTimeout` (`_fail` branch; no log; `transcribe()` raises). Unknown triggers are terminal with a warning log and return the transcript. Design rationale: with `EOT_THRESHOLD=0.7` Flux will naturally emit `model` EndOfTurns on conversational pauses; during active PTT, the user's hold is the authoritative "still talking" signal, so FRIDAY defers to it; after release, the next natural `model` EOT is accepted and `ForceEndTurn` is skipped as described in the main Decision 1 narrative.

*`FORCE_END_TURN_NO_ACTIVE_TURN` and the accumulator.* One edge case: the user holds PTT, speaks, pauses long enough for a natural `model` EOT to fire (ignored, with its transcript accumulated per the per-turn-transcript model paragraph), stays silent through PTT release and the release-grace window, FRIDAY sends `ForceEndTurn`, and Flux responds with `Warning{code=FORCE_END_TURN_NO_ACTIVE_TURN}` because no active turn remained for the force-end to terminate. The Warning handler (`agent/stt.py:511-534`) returns the joined accumulated transcripts in this case rather than empty string, so speech captured before the natural EOT is not lost. A truly silent PTT (empty accumulator) still returns `""`.

**2. During SPEAKING, `StartOfTurn` is the barge-in trigger — confirmed before cancellation.**

This aligns with ADR-0003, which reserves `StartOfTurn` for the barge-in interrupt and `EagerEndOfTurn` for speculative LLM execution. The sequence is strictly:

1. Flux emits `StartOfTurn` while the state machine is in SPEAKING.
2. FRIDAY opens a short confirmation window (initial value: 150 ms, tunable via `BARGEIN_CONFIRM_MS`). Playback continues uninterrupted during this window.
3. If sustained speech is observed within the window, FRIDAY cancels TTS playback and transitions out of SPEAKING. The cancel path is the destructive cancel from Decision 3 — the ElevenLabs stream drains, PortAudio closes via the nested try/finally pattern from #55, and SPEAKING ends.
4. If no sustained speech is observed, the `StartOfTurn` is treated as a false trigger (throat clear, Lily's own voice bleeding through, brief room noise). No state transition occurs. No cancellation runs. Playback has already been playing throughout, so nothing resumes — nothing was paused.

This ordering means the user experiences a short lag (up to `BARGEIN_CONFIRM_MS` ms) between starting to speak and Lily stopping. This is accepted for v1 (see Negative Consequences). The alternative — pausing playback immediately on `StartOfTurn` and resuming on rejection — would require a non-destructive "paused" state in the playback pump that does not exist in the Tier 3 contract and is explicitly deferred to a follow-up ADR.

`EagerEndOfTurn` and `TurnResumed` continue to serve their Decision 1 role (speculative finalization and retraction during PTT-framed turns). They are not barge-in triggers.

**3. Playback cancellation is cooperative, not forced.**

`BargeInEvent` is the signal `voice_loop` observes. During SPEAKING, `voice_loop` races `machine.barge_in_event.wait()` against `playback_task` via `asyncio.wait(FIRST_COMPLETED)` (`agent/loop.py:475-484`). When the waiter wins, `voice_loop` cancels `playback_task` directly (`agent/loop.py:504-505`). Cancellation unwinds through `audio.playback`'s task-group cleanup — the inner `_feed` task is cancelled and awaited, pending queued chunks are discarded (the pump exits without draining them), and the PortAudio stream closes through the nested `try`/`finally` pattern established in #55 (`agent/audio.py:422-453`). `voice_loop`, as the sole state owner per Decisions 4 and 5, fires the SPEAKING → BARGE_IN transition with its `BargeInEvent` payload before the cancel (`agent/loop.py:491-499`) and the BARGE_IN → LISTENING transition after the `finally` block drains the sibling tasks (`agent/loop.py:546`). The `AudioPlaybackError`/watchdog plumbing from #55 covers the hard-failure path; barge-in is the soft-cancel path.

**4. A single state machine owns voice-turn transitions.**

The current `voice_loop()` carries control flow in-line. Barge-in adds enough edges that a formal state machine is necessary:

Only the state owner mutates the state. Everything else observes.

**5. Mid-stream LLM cancellation on barge-in — v1 drops the current turn; preservation is deferred.**

Barge-in during SPEAKING cancels the in-flight LLM stream. v1 handles this as "option a": the pending user turn is dropped and the user re-speaks after being interrupted. Preserving the pending turn (option b) requires ADR-level consideration for Anthropic role-alternation semantics, capture of the barge-in-triggering utterance in `detect_barge_in`, and a stitching strategy for the partial assistant response collected before cancel; see Open Questions and the follow-up issue.

The pattern:

1. The LLM stream (`stream_sentences()`) and its downstream TTS feed are constructed inline in `voice_loop` as a lazy iterator chain: `sentence_iter = stream_sentences(messages, SYSTEM_PROMPT)` → `synth_iter = tts.synthesize(_tee(sentence_iter, collect))` → `playback_task = asyncio.create_task(audio.playback(synth_iter))`. Only `playback_task` is a dedicated subtask; the LLM stream and TTS feed are iterated lazily inside it. `audio.playback` further wraps `synth_iter` in a dedicated `_feed` task (`agent/audio.py:372-377`). Cancelling `playback_task` cancels `_feed`, which raises `CancelledError` at its `queue.put` backpressure point — inside the `async for` body, not inside the upstream iterator's `__anext__()`. Upstream iterators (`tts.synthesize`, `_tee`, `stream_sentences`) are not explicitly closed on this path; their `httpx` and SDK resources are released when CPython's refcount-based GC finalizes them, which happens promptly in practice but is not a composition guarantee. The hard-failure path from #55 (feed task raising a real exception) propagates differently and is unchanged by barge-in.
2. When `BARGE_IN` fires, only `playback_task` is cancelled via `playback_task.cancel()`, which tears down the whole LLM→TTS→playback chain by composition. The `voice_loop` task continues running — it awaits the subtask's cleanup and drives the transition to LISTENING.
3. The subtask's `CancelledError` is caught at the LLM seam and surfaced as a clean turn abort. The `httpx` cancel semantics exercised in #55 cover the actual stream close.
4. `voice_loop` distinguishes barge-in abort from error abort by the originating state (BARGE_IN), not by `task.uncancel()` return values. On barge-in abort, v1 drops both the pending user message (`messages.pop()` at `agent/loop.py:564`, with the inline comment explicitly naming the "option a" trade-off and the Anthropic role-alternation constraint behind it) and the barge-in-triggering utterance (`detect_barge_in` sets `machine.barge_in_event` and returns; it does not capture or forward the triggering utterance). Preservation (option b) is deferred — see Open Questions.
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
- Decision 1's RELEASE-grace flow depends on Flux session configuration (`eot_threshold`, `eager_eot_threshold`). The current `EOT_THRESHOLD = 0.7` + `EAGER_EOT_THRESHOLD = 0.5` satisfies the invariants, and the import-time check at `agent/stt.py:138-141` prevents the ordering invariant from silently regressing. Documenting the misconfiguration modes for reference:
    - If `eager_eot_threshold` were unset, `EagerEndOfTurn` events would not arrive. Speculative finalization and the `TurnResumed` retraction path would stop functioning; ordinary `EndOfTurn` would still fire and could land inside the grace, so the receive wait would not be unbounded, but the turn-accuracy benefits of eager mode would be lost.
    - If `eot_threshold` were set to `1.0` (ADR-0003's Tier 3 posture), Flux would suppress confidence-based `EndOfTurn` detection entirely — the confidence threshold would never be reached. Flux's separate `eot_timeout_ms` safety net (currently `30,000 ms`, `agent/stt.py:111 EOT_TIMEOUT_MS = 30000`) would fire `EndOfTurn` after that timeout regardless of confidence, but 30 seconds is well outside any plausible `PTT_RELEASE_GRACE_MS`. Under this configuration, `ForceEndTurn` would be the only path capable of completing within the grace window — the `eot_timeout_ms` fallback would still eventually fire `EndOfTurn`, but the receive wait would be bounded by the client-side watchdog (`RECEIVE_TIMEOUT_S = 35` seconds) rather than by the 500 ms grace.
    - If `eager_eot_threshold > eot_threshold`, the import-time check at `agent/stt.py:138-141` fires and FRIDAY refuses to start — a hard dev-time error, not a silent degradation.
- The voice-turn state machine is new surface area; test coverage must be strict (legal transitions + rejected illegal transitions).
- Partial ElevenLabs generations still bill when playback is cancelled. Cost is bounded by typical reply length and user interruption rate; acceptable.
- Barge-in confirmation window (`BARGEIN_CONFIRM_MS = 150` ms, `agent/stt.py:126`) is a knob. Too short → false interruptions on breath pauses. Too long → feels unresponsive. Tuning expected after real-world use.

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

- Tier 4a issues #70, #71, #72 own the implementation of this ADR.
    - #70 (STT-side event migration: natural `EndOfTurn`, eager mode, PTT-release grace) landed as PR #84.
    - #72 (voice turn state machine + mic-stays-open during SPEAKING) landed as PR #86. The planned ordering had #72 landing last to absorb #70's and #71's changes; #71 was reordered after #72 because #72's `BargeInEvent` and SPEAKING ↔ BARGE_IN state machine are prerequisites for the destructive-cancel plumbing #71 adds.
    - #71 (TTS playback cancel on barge-in — the Decision 3 destructive cancel) is open; blocked on #87 (SPEAKING-transition-after-first-chunk, filed as a #86 follow-up).
- Barge-in confirmation window: `BARGEIN_CONFIRM_MS = 150` ms (`agent/stt.py:126`). Carries a `TODO(#72-tuning)` comment because the right value is unknown until real-world use.
- PTT release grace: `PTT_RELEASE_GRACE_MS = 500` ms (`agent/stt.py:116`). Governs the bounded wait for Flux to emit `EndOfTurn` naturally after RELEASE before `ForceEndTurn` fires as the liveness fallback (Decision 1). Carries a `TODO(#70-tuning)` comment.
- Flux session configuration: `EOT_THRESHOLD = 0.7` and `EAGER_EOT_THRESHOLD = 0.5` (`agent/stt.py:101-106`), both passed to the Flux session open at `agent/stt.py:653-654`. Values are `TODO(#70-tuning)` — expected to be revisited after real-world use.
- Flux constraint `EAGER_EOT_THRESHOLD <= EOT_THRESHOLD` must hold; a higher eager value is a session-open configuration error. Enforced by the import-time `assert` at `agent/stt.py:138-141` (dev-time guard; stripped under `python -O`, which FRIDAY does not use). Reference Deepgram's Flux eager-mode guidance for the ranges that behave sensibly together.
- Flux's EOT safety net (`eot_timeout_ms`, currently `30,000 ms` per `agent/stt.py:111 EOT_TIMEOUT_MS = 30000`) is distinct from `eot_threshold`. `eot_threshold` governs confidence-based detection; `eot_timeout_ms` is a hard fallback that fires `EndOfTurn` after a fixed silence duration regardless of confidence. #70 should consider whether to lower `eot_timeout_ms` alongside `eot_threshold` so the safety net is useful inside RELEASE grace in pathological cases (e.g., prolonged background noise preventing confidence from clearing the threshold).
- The subtask-cancellation path described in Decision 5 shipped with #72 (PR #86): `playback_task = asyncio.create_task(audio.playback(synth_iter))` is scoped to the SPEAKING state at `agent/loop.py:471-474`; `voice_loop` holds the task handle and is the only caller of `playback_task.cancel()` (`agent/loop.py:504-505`); the LLM stream and TTS feed are iterated lazily inside the subtask, with `audio.playback`'s inner `_feed` task (`agent/audio.py:372-377`) sitting between the subtask and `synth_iter`. Promoting upstream iterator cleanup from "promptly via GC" to an explicit `aclose()` chain is a candidate follow-up if real-world use shows leaked connections.
- README section on headphone recommendation added as part of #72 or a follow-up docs issue.

## Open Questions (Not Blocking)

- Should FRIDAY speak a short acknowledgment when barge-in fires ("oh, sorry") or just stop? Current decision: just stop. Revisit after use.
- If the 150 ms (or tuned-lower) confirm-before-cancel lag is perceptibly bad during #70 implementation, follow up with an ADR introducing a pause-and-resume playback state so the destructive cancel can be delayed until barge-in is confirmed. Track as a tuning issue separate from #70/#71.
- PTT-during-THINKING cancellation: if the user presses PTT while FRIDAY is in THINKING (waiting for the LLM to respond), they likely want to cancel the pending turn and say something else. Not supported in v1 — Decision 5 scopes barge-in to SPEAKING only. If this gap is felt during real use, address with a follow-up that either (a) adds a THINKING-origin cancel path distinct from barge-in, or (b) extends the state machine to allow BARGE_IN from THINKING. File a tuning issue if observed.
- Should LLM-side network errors get a voice apology similar to the TTS-side apology from #55 ("Sorry, I lost my voice")? Current ADR follows the existing agent/loop.py behavior (console message, no TTS). If voice feedback for all failure classes is a UX goal, add it in a focused follow-up; the plumbing (apology-TTS path) already exists on the TTS side and can be extended to the LLM-error branch.
- **Preserve pending turn + barge-in-triggering utterance after barge-in (option b)?** Decision 5 v1 drops both. Preservation requires: (a) `detect_barge_in` capturing and returning the triggering utterance (currently just sets an event); (b) a role-alternation-safe way to append the partial `collect` as an assistant message when Claude got at least one sentence out before cancel, interacting with ADR-0007's context-rot semantics; (c) `voice_loop` feeding the captured utterance as the next turn's input instead of waiting for a fresh PTT. Non-trivial; needs its own ADR consideration. Tracked as #91.

## References

- Deepgram Flux docs — EagerEndOfTurn and TurnResumed events: <https://developers.deepgram.com/docs/flux>
- PR #55 — Graceful error handling for voice pipeline (introduces `AudioPlaybackError`, playback dual-watchdog, nested-finally stream teardown, and `httpx` mid-stream cancellation path that this ADR builds on).
- ADR-0003 — Voice architecture (the pre-commitment this ADR cashes in).
- ADR-0003 §Tier-4 migration path for STT — defines the Flux event vocabulary (StartOfTurn / EagerEndOfTurn / TurnResumed / EndOfTurn) and reserves StartOfTurn for the barge-in interrupt.
