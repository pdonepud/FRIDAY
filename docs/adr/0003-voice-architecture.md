# ADR 0003 — Voice architecture

**Status:** Accepted
**Date:** 2026-09-03

## Context

Tier 3 introduces the voice-first interaction model that defines FRIDAY. Tier 2 shipped a text-only terminal loop; Tier 3 replaces that default with push-to-talk voice while preserving text as a debug fallback.

Voice pipelines have latency budgets that text loops don't. Every stage (capture, STT, LLM, TTS, playback) must stream, and stages must overlap. A non-streaming or sequentially-blocking design would be audibly wrong from the first turn and expensive to retrofit. The architecture below is chosen to make the streaming, overlapping shape idiomatic from day one.

Constraints that shaped these decisions:

- Minimal dependencies (project principle) — every added dep is justified below.
- Windows-first (Preetam's dev environment), cross-platform-friendly where free.
- API keys handled only by Preetam directly; lazy-init pattern from `claude.py`.
- No GUI, no wake word, no barge-in, no tool use in Tier 3 — those are Tier 4+.

## Decision

### Concurrency: asyncio

The voice loop runs on a single asyncio event loop. Mic capture, STT streaming, LLM streaming, TTS streaming, and playback are all async generators/coroutines composed via `async for`.

Rationale: Deepgram and ElevenLabs SDKs are async-first; `sounddevice` and `pynput` bridge cleanly to asyncio via queues. A threading + `queue.Queue` design would work but requires manual bridging at every seam. asyncio keeps the pipeline legible as one linear composition of streams.

### Streaming end-to-end

Every stage streams:

```
PTT press → mic (PCM chunks) → Deepgram (transcript) → Claude (tokens)
         → sentence buffer (phrases) → ElevenLabs (PCM chunks) → speaker
```

The sentence-buffer stage exists because token-level output produces one-word-at-a-time TTS input, which sounds wrong. Flushing on sentence/clause boundaries (`[.!?]\s+`, `[,;:]\s+`, buffer > N chars, or upstream close) gives ElevenLabs enough context to synthesize natural prosody without meaningfully hurting time-to-first-audio.

### LLM stage: `AsyncAnthropic` client

The current `agent/claude.py::stream_reply` uses synchronous iteration over `client.messages.stream(...)`. Reusing that seam on the voice event loop would block mic capture, STT streaming, and TTS playback while Claude streams — the whole pipeline serializes on one sync generator.

The fix is to swap the Anthropic client for `AsyncAnthropic`, exposed via the SDK's async surface. `async for event in stream:` composes cleanly with the rest of the pipeline; no worker threads, no `asyncio.to_thread` wrapper, no manual cancellation queue.

This migration lands in #52 (previously scoped only to sentence-chunked streaming; scope now includes the sync → async client swap). The `UP028` exemption in `pyproject.toml` is removed as a natural side effect: the `yield from` refactor becomes idiomatic once the async generator wraps the async client stream.

Text mode migrates alongside voice: `stream_reply` becomes an async generator (single seam preserved per ADR-0002), and the text loop wraps with `asyncio.run(text_loop())` in `__main__.py`. Both the `--text` REPL and the voice loop share the same async generator against the same `AsyncAnthropic` client. Text mode gains an asyncio runtime it doesn't strictly need; the alternative (dual sync + async client paths, two `stream_reply` variants) would break the single-seam principle.

### Input: push-to-talk on Right Alt via `pynput`

Right Alt as the PTT key (matches Preetam's preference from project memory). `pynput` chosen over `keyboard` because:

- No admin required on Windows (verified dev environment). macOS requires accessibility permission; Linux support depends on backend (`uinput` requires root, X11 works unprivileged, Xwayland event coverage limited) and is unverified for FRIDAY.
- Cross-platform (macOS support if ever needed).
- Cleaner listener-callback model to bridge into asyncio.

Trade-off: `pynput` is a larger dep than `keyboard`. Accepted because the admin-elevation requirement of `keyboard` is a worse user experience.

### Audio I/O: `sounddevice`

Thin Python wrapper over PortAudio. Covers both mic capture and speaker playback with one dep. Async-friendly via callback + queue bridging. Handles Windows WASAPI, Linux ALSA/PulseAudio, macOS CoreAudio uniformly.

### STT: Deepgram Flux (`flux-general-en`), streaming, PTT-driven turn endings

Flux is Deepgram's voice-agent-purposed streaming model — same transcription quality as Nova-3, positioned by Deepgram as the successor for interactive turn-based agents. Accessed via `deepgram-sdk`'s `client.listen.v2.connect()` against the `/v2/listen` endpoint.

Turn endings are driven by PTT release using Deepgram's documented "Bring Your Own Turn Detection" recipe:

- `eot_threshold=1.0` suppresses Flux's native end-of-turn confidence detection so the model never ends a turn on its own.
- On PTT release, send a `ForceEndTurn` control message. Flux finalizes the turn on audio transcribed so far and emits `EndOfTurn` with `trigger: "manual"`.
- `eot_timeout_ms` set as a safety-net backstop (30s) so a stuck session cannot hang the loop indefinitely if a PTT release event is lost.

Migration path to barge-in (Tier 4): the provider stays on Flux and the endpoint stays on `/v2/listen` — no rewrite of the STT stage. Two independent configuration changes and an application-layer state machine land in Tier 4:

- **Config change 1:** lower `eot_threshold` from `1.0` back toward the default (~0.7) so Flux resumes natural end-of-turn detection. Required because Tier 4 drops PTT as the sole turn-ending signal.
- **Config change 2:** set `eager_eot_threshold` to enable `EagerEndOfTurn` events. Independent of `eot_threshold`; controls whether Flux emits early-turn guesses at all.

Application-layer handlers Tier 4 must implement, one per Flux event:

- **`StartOfTurn`** — user began speaking during an agent response. Interrupt active ElevenLabs playback and clear the sentence buffer.
- **`EagerEndOfTurn`** — Flux's early guess the turn is ending. Optionally begin speculative `AsyncAnthropic` generation.
- **`TurnResumed`** — user kept speaking after an eager fire. Cancel any speculative `AsyncAnthropic` stream started at the previous `EagerEndOfTurn`.
- **`EndOfTurn`** — turn finalized. Dispatch the finalized transcript to `AsyncAnthropic` (non-speculative path) and let the response stream to ElevenLabs.

Choosing Flux now avoids the provider migration; the barge-in state machine is Tier 4 work regardless of STT choice.

### TTS: ElevenLabs, streaming, British female voice

Streaming synthesis so playback starts before Claude finishes. The specific voice ID is selected during #51 implementation and recorded in a follow-up ADR at that time — per this repo's ADR-immutability rule, it is not appended to this document.

### Config: lazy-init per module

Each provider module (`stt.py`, `tts.py`) exposes a `_get_client()` mirroring `claude.py`. Environment variables:

- `ANTHROPIC_API_KEY` (existing)
- `DEEPGRAM_API_KEY` (new)
- `ELEVENLABS_API_KEY` (new)

`.env.example` and README updated in #54.

### Text mode preserved

`python -m agent --text` preserves the Tier 2 text REPL's user-facing contract: streaming token output, prompt returns between turns, Ctrl+C exits cleanly. Runtime changes underneath — the loop now runs under `asyncio.run(text_loop())` and consumes the async `stream_reply` generator against `AsyncAnthropic` (see "LLM stage" above). User behavior unchanged; implementation and runtime changed.

Text mode also serves as automatic fallback if the mic device is unavailable at startup (#55).

### New dependencies (batch-approved)

| Package         | Purpose               | Justification                                    |
|-----------------|-----------------------|--------------------------------------------------|
| `sounddevice`   | Mic + speaker I/O     | Only viable cross-platform PortAudio wrapper.    |
| `pynput`        | Global Right Alt PTT  | Global Right Alt hotkey; asyncio-bridgeable via listener callback. Platform support varies (see Input subsection). |
| `deepgram-sdk`  | Streaming STT         | Native streaming client for chosen STT provider. |
| `elevenlabs`    | Streaming TTS         | Native streaming client for chosen TTS provider. |

Each replaces something we'd otherwise write by hand against a raw WebSocket or OS API. All four are widely maintained.

## Consequences

**Positive:**

- Streaming-native architecture; latency budget is spendable, not owed.
- Idiomatic asyncio composition; the pipeline reads top-to-bottom in `loop.py`.
- Text mode survives as first-class dev/debug affordance.
- Removes Tier-2 lint/format exemptions naturally as a side effect of #52 and #53 (see #39).

**Negative:**

- Four new dependencies land in one tier — largest dep expansion in project history. Justified individually above.
- `pynput` global listener requires accessibility permissions on macOS if Preetam ever moves off Windows.
- ElevenLabs and Deepgram usage costs money per turn; a runaway loop is now a billing event, not just wasted CPU.

**Explicit non-goals for Tier 3:**

Deferred to Tier 4 or later:

- **Barge-in / interruption** — needs interim STT results and TTS cancellation mid-stream.
- **Wake word** — deferred by project decision; PTT sufficient.
- **Voice activity detection** — unnecessary with PTT.
- **Tool use** (Canvas, stocks, traffic) — needs stable voice loop first.
- **Multi-provider LLM routing** — ADR-0002 defers LiteLLM; no change here.

## Alternatives Considered

**Deepgram Nova-3 for STT.** Rejected: Nova-3 is Deepgram's general-purpose streaming model on `/v1/listen` with no built-in turn-taking primitives. Flux is the voice-agent-purposed model with the same transcription quality, a `ForceEndTurn` control message designed specifically for push-to-talk release, and a documented forward path to barge-in via `eager_eot_threshold` in Tier 4. Choosing Nova-3 now would mean re-migrating to Flux when barge-in lands — Flux from day one avoids the churn.

**Threading + `queue.Queue` instead of asyncio.** Rejected: works but requires manual thread-to-thread bridging at every seam and interacts awkwardly with the async-first SDKs.

**`keyboard` instead of `pynput` for PTT.** Rejected: requires admin on Windows for global hotkeys.

**Local Whisper for STT.** Rejected for Tier 3: adds model-management complexity and CPU/GPU cost without latency win over Deepgram streaming. Revisit if Deepgram cost or privacy becomes a concern.

**Local Piper/Coqui for TTS.** Rejected for Tier 3: voice quality gap vs ElevenLabs is large enough to hurt the product feel this early. Revisit once the product has legs.

**Non-streaming turn model** (record full utterance → transcribe → generate full response → synthesize → play). Rejected: latency would be several seconds per turn, defeating the voice-first premise.

## References

- `AGENT.md` — voice-first pivot
- `agent/claude.py` — the thin-seam pattern being extended to `stt.py` / `tts.py`
- ADR 0001 — voice-first pivot (why voice is Tier 3, not Tier 6)
- ADR 0002 — model routing strategy (why we're not adding LiteLLM as part of the voice-provider expansion)
- Issue #47 — this ADR
- Issues #48–#55 — Tier 3 implementation work
- Issue #39 — remove Tier-2 lint/format exemptions (naturally satisfied by #52 + #53)
