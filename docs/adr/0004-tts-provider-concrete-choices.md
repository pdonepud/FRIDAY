# ADR 0004 — TTS provider concrete choices

**Status:** Accepted
**Supersedes / refines:** ADR-0003 (voice architecture) §TTS
**Date:** 2026-09-17

## Context

Issue #51's body said "document in ADR-0003 addendum." ADR-0003's own text,
however, commits to this repo's ADR-immutability rule and directs the choice
into "a follow-up ADR at that time" (ADR-0003 §TTS). This ADR follows
ADR-0003, not the issue body: the choice lives here as a new ADR, and
ADR-0003 is untouched.

ADR-0003 locked in ElevenLabs streaming with a British female voice but
deliberately left the voice ID, model ID, output format, and transport open
to be recorded here. This ADR records the concrete choices made during #51
implementation.

## Decision

The following are the canonical values. `agent/tts.py` mirrors them as module
constants; ADR is the source of truth if the two ever disagree.

- **SDK:** `elevenlabs==2.68.0` (pinned in `agent/requirements.txt`). Used
  only for the `AsyncElevenLabs` client scaffolding and error types; the
  wire protocol is ours.
- **Transport:** raw async WebSocket against
  `/v1/text-to-speech/{voice_id}/stream-input`, via
  `websockets.asyncio.client.connect` from `websockets==16.0` (explicit pin;
  already a transitive dep from `deepgram-sdk` and `elevenlabs`, and
  `agent/stt.py` already imports from `websockets.exceptions`).
- **Voice ID:** `pFZP5JQG7iQjIQuC4Bku`
- **Voice name / metadata:** Lily — "Velvety Actress." Premade voice; British
  female; confident; middle-aged.
- **Model ID:** `eleven_turbo_v2_5`. Low-latency, valid on the stream-input
  endpoint. `eleven_v3` is NOT valid on stream-input and is not a candidate.
- **Output format:** `pcm_24000` — 24 kHz mono s16le. Byte-for-byte match
  with `agent/audio.py`'s `OUTPUT_SAMPLE_RATE / OUTPUT_CHANNELS / OUTPUT_DTYPE`.
  Not tier-gated (`pcm_44100` is Pro-only; `pcm_24000` is available on all
  tiers).
- **Inactivity guards (client + server):**
  - `PROVIDER_INACTIVITY_TIMEOUT_S = 30` — sent as a query parameter
    on the stream-input WebSocket alongside `model_id` and
    `output_format`. The blog post
    [WebSocket improvements: reliability & custom timeout](https://elevenlabs.io/blog/websocket-improvements-reliability-and-custom-timeout)
    documents the server-side default as 20 seconds and the maximum
    as 180 seconds (unit: seconds; example URL:
    `wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input?model_id=eleven_turbo_v2&inactivity_timeout=180`).
    Sending 30 reserves a 5-second window before the server-side
    idle-close so the client-side timer fires first.
  - `INACTIVITY_TIMEOUT_S = 25` — client-side guard on the receive
    loop. Set 5 s below `PROVIDER_INACTIVITY_TIMEOUT_S` so the client
    surfaces a clean typed `TTSError("no audio within 25s")` before
    the server's own idle-close races us. `INACTIVITY_TIMEOUT_S`
    remains the source of truth for the wording embedded in the
    error message.
- **Init frame:** `{"text": " ", "generation_config": {"chunk_length_schedule": [50]}}`.
  Aggressive `chunk_length_schedule=[50]` matches the SDK's own
  `elevenlabs/realtime_tts.py:123` and prioritizes time-to-first-audio over
  buffer-fill quality.
- **Text frame:** `{"text": <chunk>, "try_trigger_generation": True}` on
  every frame. Matches the SDK's `elevenlabs/realtime_tts.py:132`.
- **End frame:** `{"text": ""}`.

### `auto_mode`

`auto_mode` investigated at #51 implementation time; not present in the
Fern-generated schema at `elevenlabs==2.68.0`
(`types/initialize_connection.py`, `types/generation_config.py`), not sent
as a query param by the SDK (`realtime_tts.py:102-104`), and not documented
at any reachable ElevenLabs docs URL (all candidate paths returned 404).
Deferred; revisit if a citable source surfaces.

Fallback path locked: `chunk_length_schedule=[50]` in the init frame plus
`try_trigger_generation=True` on every text frame achieves the same "generate
immediately, don't defer" behavior the `auto_mode` name suggests. This is
precisely what the SDK's own sync `convert_realtime` does.

## Consequences

**Positive:**

- Playback starts before Claude finishes generating a sentence, per
  ADR-0003 §Streaming end-to-end.
- Async-native concurrency; no thread bridging (matches ADR-0003
  §Concurrency, which explicitly rejects the threading + queue.Queue
  alternative).
- Sender death is detected within one event-loop tick via a pcm-queue
  sentinel; a broken text source does not burn credits for
  `INACTIVITY_TIMEOUT_S` before surfacing.
- 24 kHz output byte-matches the playback pipeline — no resampling, no
  format conversion.
- Server-side idle-close no longer races the client-side timer. The
  server closes at `PROVIDER_INACTIVITY_TIMEOUT_S = 30 s`, the client
  fires at `INACTIVITY_TIMEOUT_S = 25 s`, and the 5 s window means
  callers see `TTSError("no audio within 25s")` — a typed error whose
  `__cause__` is a `TimeoutError` — rather than a `ConnectionClosed`
  from the server pulling the plug first. The safety property
  survives event-loop scheduling jitter (measured ≪ 5 s on all tested
  platforms).

**Negative:**

- We own the wire protocol (base64 decode, frame dispatch, init/text/end
  message shapes). ~150 lines of transport code to maintain. The SDK's own
  `convert_realtime` is sync-only (`elevenlabs/realtime_tts.py:49-152`,
  uses `websockets.sync.client.connect`) and is not wired into
  `AsyncElevenLabs`, so we can't share its implementation.
- `websockets==16.0` is now a direct dependency (previously transitive).
  Version bumps of `deepgram-sdk` or `elevenlabs` that tighten their
  `websockets` constraints need to be checked against this pin.
- `socket._websocket` iteration in `agent/stt.py` and the raw WS in
  `agent/tts.py` both couple us to `websockets`' API stability — the pin
  bounds that coupling. Migration guidance if either SDK ships an async
  stream-input surface: revisit and delete our transport code.

## Alternatives considered

- **Sync `convert_realtime` in a worker thread with queue bridging.**
  Rejected: threading contradicts ADR-0003 §Concurrency (which explicitly
  rejects the threading + queue.Queue alternative). Cancellation across
  the thread boundary is awkward — asyncio can't cancel the worker's
  blocking `socket.recv`.
- **Sentence-buffer + async HTTP `stream()` per sentence.** Rejected:
  higher time-to-first-audio (waits for the full sentence before opening
  the HTTP request). No mid-sentence streaming. Simpler, but ADR-0003
  §Streaming end-to-end specifically calls for playback to start before
  the sentence is complete.

## References

- Issue #51 — implementation
- ADR 0003 — voice architecture (§TTS)
- `agent/tts.py` — canonical constants (mirrors this ADR)
- ElevenLabs voices dashboard — <https://elevenlabs.io/app/voice-lab>
- ElevenLabs WebSocket docs — <https://elevenlabs.io/docs/websockets>
- `elevenlabs==2.68.0` Fern-generated schema:
  - `elevenlabs/types/initialize_connection.py`
  - `elevenlabs/types/generation_config.py`
  - `elevenlabs/types/send_text.py`
  - `elevenlabs/realtime_tts.py` (SDK's own sync implementation, for
    protocol reference)
