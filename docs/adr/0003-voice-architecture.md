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

The sentence-buffer stage exists because token-level output produces one-word-at-a-time TTS input, which sounds wrong. Flushing on sentence/clause boundaries (`[.!?]\s+`, `[,;:]\s+`, or buffer > N chars) gives ElevenLabs enough context to synthesize natural prosody without meaningfully hurting time-to-first-audio.

### Input: push-to-talk on Right Alt via `pynput`

Right Alt as the PTT key (matches Preetam's preference from project memory). `pynput` chosen over `keyboard` because:

- No admin/root required on Windows or Linux.
- Cross-platform (macOS support if ever needed).
- Cleaner listener-callback model to bridge into asyncio.

Trade-off: `pynput` is a larger dep than `keyboard`. Accepted because the admin-elevation requirement of `keyboard` is a worse user experience.

### Audio I/O: `sounddevice`

Thin Python wrapper over PortAudio. Covers both mic capture and speaker playback with one dep. Async-friendly via callback + queue bridging. Handles Windows WASAPI, Linux ALSA/PulseAudio, macOS CoreAudio uniformly.

### STT: Deepgram Nova-3, streaming

Streaming transcription with `interim_results=False` for Tier 3. Interim results enable barge-in and lower perceived latency but add state-machine complexity. Deferred to Tier 4 alongside barge-in itself.

Model choice: **Nova-3** is Deepgram's current general-purpose streaming model and the correct fit for Tier 3's PTT model where the turn boundary is deterministic (mic released ⇒ finalize). Deepgram's newer `flux-general-en` model bundles built-in turn detection tuned for voice agents; that feature only becomes useful when Tier 4 adds barge-in and drops PTT, so migration to Flux is deferred to that tier.

### TTS: ElevenLabs, streaming, British female voice

Streaming synthesis so playback starts before Claude finishes. The specific voice ID is selected during #51 implementation and recorded in a follow-up ADR at that time — per this repo's ADR-immutability rule, it is not appended to this document.

### Config: lazy-init per module

Each provider module (`stt.py`, `tts.py`) exposes a `_get_client()` mirroring `claude.py`. Environment variables:

- `ANTHROPIC_API_KEY` (existing)
- `DEEPGRAM_API_KEY` (new)
- `ELEVENLABS_API_KEY` (new)

`.env.example` and README updated in #54.

### Text mode preserved

`python -m agent --text` runs the Tier 2 text loop unchanged. Voice is the new default. Text mode also serves as automatic fallback if the mic device is unavailable at startup (#55).

### New dependencies (batch-approved)

| Package         | Purpose               | Justification                                    |
|-----------------|-----------------------|--------------------------------------------------|
| `sounddevice`   | Mic + speaker I/O     | Only viable cross-platform PortAudio wrapper.    |
| `pynput`        | Global Right Alt PTT  | No-admin, cross-platform, asyncio-bridgeable.    |
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

**Threading + `queue.Queue` instead of asyncio.** Rejected: works but requires manual thread-to-thread bridging at every seam and interacts awkwardly with the async-first SDKs.

**`keyboard` instead of `pynput` for PTT.** Rejected: requires admin on Windows for global hotkeys.

**Local Whisper for STT.** Rejected for Tier 3: adds model-management complexity and CPU/GPU cost without latency win over Deepgram streaming. Revisit if Deepgram cost or privacy becomes a concern.

**Local Piper/Coqui for TTS.** Rejected for Tier 3: voice quality gap vs ElevenLabs is large enough to hurt the product feel this early. Revisit once the product has legs.

**Non-streaming turn model** (record full utterance → transcribe → generate full response → synthesize → play). Rejected: latency would be several seconds per turn, defeating the voice-first premise.

**Deepgram Flux (`flux-general-en`) for STT.** Deferred, not rejected. Flux's built-in turn detection is tuned for voice-agent barge-in; Tier 3's PTT model already provides a deterministic turn boundary, so Flux's headline feature is idle here while its newness carries the usual early-adopter risk. Migration is a natural fit when Tier 4 adds barge-in and drops PTT.

## References

- `AGENT.md` — voice-first pivot
- `agent/claude.py` — the thin-seam pattern being extended to `stt.py` / `tts.py`
- ADR 0001 — voice-first pivot (why voice is Tier 3, not Tier 6)
- ADR 0002 — model routing strategy (why we're not adding LiteLLM as part of the voice-provider expansion)
- Issue #47 — this ADR
- Issues #48–#55 — Tier 3 implementation work
- Issue #39 — remove Tier-2 lint/format exemptions (naturally satisfied by #52 + #53)
