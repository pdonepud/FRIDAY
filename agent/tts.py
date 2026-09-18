"""ElevenLabs streaming TTS via WebSocket stream-input.

Sole importer of ``elevenlabs`` in the codebase per ADR-0003 §TTS.
The concrete choices — voice, model, output format, transport — live
in ADR-0004; the ADR is the source of truth, the module constants
below mirror it.

Public API is one coroutine (``synthesize``) plus a typed exception
hierarchy (``TTSError`` and subclasses). The ElevenLabs SDK is used
only for the ``AsyncElevenLabs`` client scaffolding and its error
types; the wire protocol on ``/v1/text-to-speech/{voice_id}/stream-input``
is ours.

Streaming shape: two concurrent asyncio tasks — a ``_sender`` that
consumes ``text_chunks`` and forwards word-boundary-clean text frames,
and a ``_receiver`` that iterates the raw WebSocket and pushes decoded
PCM bytes onto a bounded ``asyncio.Queue``. The async generator yields
from that queue. Sender death is detected within one event-loop tick
via a sentinel put on the queue — no waiting for
``INACTIVITY_TIMEOUT_S``.

Audio format is a byte-for-byte match with ``agent.audio``'s playback:
24 kHz mono s16le. No resampling. Frame alignment (odd-length audio
bytes across frame boundaries) is handled by ``agent.audio.playback``;
this module passes bytes through as received.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import sys
from collections.abc import AsyncIterator
from urllib.parse import urlencode

from elevenlabs import AsyncElevenLabs
from elevenlabs.core.api_error import ApiError
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, WebSocketException

__all__ = [
    "INACTIVITY_TIMEOUT_S",
    "MODEL_ID",
    "OUTPUT_FORMAT",
    "PCM_QUEUE_MAX",
    "TTSAuthError",
    "TTSError",
    "VOICE_ID",
    "synthesize",
]

_log = logging.getLogger(__name__)

# --- Concrete choices (ADR-0004 is the source of truth; these mirror it) ---

# Voice: Lily — Velvety Actress. Premade, British female, confident,
# middle-aged. Selected via scratchpad audition scripts against the
# ELEVENLABS_API_KEY-holder's account.
VOICE_ID = "pFZP5JQG7iQjIQuC4Bku"
MODEL_ID = "eleven_turbo_v2_5"
# 24 kHz mono s16le — byte-for-byte match with agent.audio.OUTPUT_*.
OUTPUT_FORMAT = "pcm_24000"

# --- Timing + queue bounds ---

# Server closes idle stream-input connections at ~20 s
# (https://elevenlabs.io/docs/websockets: "The WebSocket connection
# will automatically close after 20 seconds of inactivity."). Our
# client-side guard fires slightly earlier so we surface a clean
# TTSError before the server-side close races us. Hoisted so tests
# can monkeypatch small.
INACTIVITY_TIMEOUT_S = 25

# Bound on the internal PCM queue between the receiver task and the
# async generator's yield loop. Real-time playback keeps this shallow
# in practice; the bound exists to prevent unbounded growth if a
# consumer stalls.
PCM_QUEUE_MAX = 96

# The Fern-generated schema at elevenlabs==2.68.0 lives at
# elevenlabs/types/generation_config.py. chunk_length_schedule=[50]
# matches the SDK's own realtime_tts.py:123. Values ≥50 per that
# type's field docstring.
_CHUNK_LENGTH_SCHEDULE = [50]


# --- Exceptions ---


class TTSError(Exception):
    """Base class for all errors surfaced by ``synthesize``."""


class TTSAuthError(TTSError):
    """``ELEVENLABS_API_KEY`` is missing, unset, empty, or invalid.

    Raised at ``synthesize`` entry if the env var is empty/unset, and
    from the WebSocket handshake if the server rejects the key (401).
    """


# --- Lazy client (mirrors agent.claude._get_client / agent.stt._get_client) ---

_client: AsyncElevenLabs | None = None


def _get_client() -> AsyncElevenLabs:
    """Return the shared ElevenLabs client, constructing it on first use.

    Lazy so importing this module has no side effects. The client is
    kept as scaffolding for future SDK-based operations (voice enum,
    account info); ``synthesize`` bypasses it and speaks the raw
    WebSocket protocol directly.
    """
    global _client
    if _client is None:
        _client = AsyncElevenLabs()
    return _client


# --- URL + auth helpers ---


def _ws_url(voice_id: str, model_id: str, output_format: str) -> str:
    """Build the stream-input WebSocket URL with query params."""
    query = urlencode({"model_id": model_id, "output_format": output_format})
    return f"wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input?{query}"


def _auth_headers() -> dict[str, str]:
    """Return the header dict carrying the API key.

    Kept separate so we never log the header contents alongside the
    frame contents by accident.
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise TTSAuthError("ELEVENLABS_API_KEY missing (unset or empty); see .env.example.")
    return {"xi-api-key": api_key}


def _init_message() -> str:
    """Return the JSON-serialized init frame per the SDK's Fern schema."""
    return json.dumps(
        {
            "text": " ",
            "generation_config": {"chunk_length_schedule": _CHUNK_LENGTH_SCHEDULE},
        }
    )


# --- Text chunking (word-boundary clean before send) ---
# Mirrors the SDK's own text_chunker at elevenlabs/realtime_tts.py:25-40 —
# buffers until a splitter is seen, then yields with a trailing space so
# the server sees well-formed word boundaries.
_SPLITTERS = (".", ",", "?", "!", ";", ":", "—", "-", "(", ")", "[", "]", "}", " ")


async def _text_chunker(chunks: AsyncIterator[str]) -> AsyncIterator[str]:
    """Buffer text chunks and emit them at word/splitter boundaries.

    Each yielded chunk ends in a space so the receiver sees well-formed
    word boundaries. LLM deltas that split mid-word ("Hel", "lo the",
    "re.") are recombined here rather than sent as-is.
    """
    buffer = ""
    async for text in chunks:
        if buffer.endswith(_SPLITTERS):
            yield buffer if buffer.endswith(" ") else buffer + " "
            buffer = text
        elif text.startswith(_SPLITTERS):
            output = buffer + text[0]
            yield output if output.endswith(" ") else output + " "
            buffer = text[1:]
        else:
            buffer += text
    if buffer:
        yield buffer if buffer.endswith(" ") else buffer + " "


async def _peek_nonempty(chunks: AsyncIterator[str]) -> str | None:
    """Consume ``chunks`` until a non-whitespace chunk is seen.

    Returns that chunk as ``first_text``. The caller prepends it back
    onto ``chunks`` via ``_prepend``. If the iterator exhausts with
    only empty or whitespace-only chunks, returns ``None`` — the
    caller then returns without opening the WebSocket.

    Leading whitespace-only chunks BEFORE the first real text are
    silently consumed; whitespace WITHIN a real chunk is preserved
    and handed to the chunker unchanged.
    """
    async for chunk in chunks:
        if chunk.strip():
            return chunk
    return None


async def _prepend(first: str, rest: AsyncIterator[str]) -> AsyncIterator[str]:
    """Yield ``first``, then continue iterating ``rest``."""
    yield first
    async for chunk in rest:
        yield chunk


# --- Public API ---


async def synthesize(text_chunks: AsyncIterator[str]) -> AsyncIterator[bytes]:
    """Stream text into ElevenLabs; yield PCM audio bytes as they arrive.

    Opens a WebSocket to ``/v1/text-to-speech/{VOICE_ID}/stream-input``
    only if ``text_chunks`` yields at least one non-whitespace chunk.
    Empty or whitespace-only input returns immediately without opening
    a connection — no synthesis credits spent.

    Args:
        text_chunks: async iterator yielding text fragments. Mid-word
            splits are OK; internal chunking recombines to word
            boundaries before sending.

    Yields:
        Raw PCM bytes at 24 kHz mono s16le. Chunk sizes vary with the
        server's generation schedule; downstream ``agent.audio.playback``
        handles odd-length chunks via its frame-alignment logic.

    Raises:
        TTSAuthError: ELEVENLABS_API_KEY is missing/empty at entry, or
            the server rejects the key with 401.
        TTSError: any other pipeline failure — server error frame,
            connection closed mid-stream, inactivity timeout, text
            source raised, or ApiError from client construction.
    """
    # Fail fast on missing key BEFORE peeking chunks (so an empty
    # iterator with no key still gives a clear auth error).
    headers = _auth_headers()

    first_text = await _peek_nonempty(text_chunks)
    if first_text is None:
        _log.debug("tts: empty/whitespace-only input; no ws opened")
        return

    try:
        async with ws_connect(
            _ws_url(VOICE_ID, MODEL_ID, OUTPUT_FORMAT),
            additional_headers=headers,
        ) as ws:
            await ws.send(_init_message())

            pcm_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=PCM_QUEUE_MAX)
            pipeline_error: list[BaseException | None] = [None]

            async def _sender() -> None:
                try:
                    async for chunk in _text_chunker(_prepend(first_text, text_chunks)):
                        await ws.send(json.dumps({"text": chunk, "try_trigger_generation": True}))
                    await ws.send(json.dumps({"text": ""}))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if pipeline_error[0] is None:
                        err = TTSError("text source failed")
                        err.__cause__ = exc
                        pipeline_error[0] = err
                    # Unblock the yield loop within one event-loop tick.
                    await pcm_queue.put(None)

            async def _receiver() -> None:
                try:
                    async with asyncio.timeout(INACTIVITY_TIMEOUT_S):
                        async for raw in ws:
                            frame = json.loads(raw)
                            server_err = frame.get("error")
                            if server_err:
                                raise TTSError(f"server error: {server_err}")
                            audio_b64 = frame.get("audio")
                            if audio_b64:
                                data = base64.b64decode(audio_b64)
                                _log.debug("tts recv: audio %d bytes", len(data))
                                await pcm_queue.put(data)
                            else:
                                # Alignment-only frames (alignment /
                                # normalizedAlignment with no audio and
                                # no isFinal) fall through to here — log
                                # and keep listening.
                                _log.debug(
                                    "tts recv: non-audio frame keys=%r",
                                    sorted(frame.keys()),
                                )
                            if frame.get("isFinal"):
                                break
                except asyncio.CancelledError:
                    raise
                except TTSError as exc:
                    if pipeline_error[0] is None:
                        pipeline_error[0] = exc
                except (ConnectionClosed, WebSocketException) as exc:
                    if pipeline_error[0] is None:
                        err = TTSError("connection closed")
                        err.__cause__ = exc
                        pipeline_error[0] = err
                except TimeoutError as exc:
                    if pipeline_error[0] is None:
                        err = TTSError(f"no audio within {INACTIVITY_TIMEOUT_S}s")
                        err.__cause__ = exc
                        pipeline_error[0] = err
                except Exception as exc:
                    if pipeline_error[0] is None:
                        err = TTSError("receive loop failed")
                        err.__cause__ = exc
                        pipeline_error[0] = err
                finally:
                    await pcm_queue.put(None)

            sender_task = asyncio.create_task(_sender(), name="tts-sender")
            receiver_task = asyncio.create_task(_receiver(), name="tts-receiver")
            tasks = (sender_task, receiver_task)

            try:
                while True:
                    chunk = await pcm_queue.get()
                    if chunk is None:
                        break
                    yield chunk
            finally:
                for t in tasks:
                    if not t.done():
                        t.cancel()
                for t in tasks:
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
                    except BaseException as e:  # noqa: BLE001
                        _log.warning(
                            "tts: task %s raised during drain: %r",
                            t.get_name(),
                            e,
                        )
                # Only surface a pipeline error if the caller isn't
                # already unwinding an in-flight exception
                # (GeneratorExit from aclose, or an exception the
                # consumer raised inside their ``async for`` body).
                if pipeline_error[0] is not None and sys.exc_info()[1] is None:
                    raise pipeline_error[0]
    except ConnectionClosed as exc:
        # Handshake-level closes (e.g. 401 from the server after headers
        # are inspected) surface here rather than inside the receiver
        # task. websockets 16.0 raises ConnectionClosed with .rcvd
        # carrying a Close frame that may include the HTTP status.
        raise _wrap_handshake_close(exc) from exc
    except WebSocketException as exc:
        raise TTSError("connection failed") from exc
    except OSError as exc:
        raise TTSError("connection failed") from exc
    except ApiError as exc:
        raise _wrap_api_error(exc) from exc


def _wrap_handshake_close(exc: ConnectionClosed) -> TTSError:
    """Translate a handshake-time close into TTSAuthError / TTSError.

    Some websockets versions raise ConnectionClosed with a Close frame
    carrying the HTTP status code from a failed handshake; 401 there
    means the server rejected the API key.
    """
    rcvd = getattr(exc, "rcvd", None)
    code = getattr(rcvd, "code", None)
    if code == 1008 or code == 3401:
        # 1008 (policy violation) / 3401 are ways servers signal auth
        # rejection over websockets close frames in the wild. Not a
        # committed schema but common enough to check first.
        return TTSAuthError("ELEVENLABS_API_KEY rejected by server; see .env.example.")
    err = TTSError(f"connection closed during handshake (code={code!r})")
    return err


def _wrap_api_error(exc: ApiError) -> TTSError:
    """Translate an ElevenLabs ``ApiError`` into our hierarchy."""
    status = getattr(exc, "status_code", None)
    if status == 401:
        return TTSAuthError("ELEVENLABS_API_KEY invalid (401 from ElevenLabs); see .env.example.")
    return TTSError(f"ElevenLabs API error (status={status})")


# --- __main__ helpers ---
# The ``if __name__ == "__main__":`` dispatch below carries a
# ``# pragma: no cover`` per the precedent set in #48. The ``_main``
# coroutine itself is unit-tested with monkey-patched deps.


# 2 sentences, one mid-word split, deliberately word-boundary-messy so
# the internal text_chunker gets a real workout at manual-test time.
_MAIN_TEXT_CHUNKS = [
    "Good morning, Preetam. ",
    "It's Fri",
    "day and I'm rea",
    "dy to help you today.",
]


async def _iter_text_chunks() -> AsyncIterator[str]:
    for chunk in _MAIN_TEXT_CHUNKS:
        yield chunk


async def _main() -> None:
    """Feed a fixed 2-sentence line through synthesize + audio.playback.

    Single-utterance by design; multi-utterance loop belongs to #53.
    """
    from agent import audio

    print("[tts] synthesizing…", file=sys.stderr)

    pcm_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=PCM_QUEUE_MAX)

    async def _synth_task() -> None:
        try:
            async for pcm in synthesize(_iter_text_chunks()):
                await pcm_queue.put(pcm)
        finally:
            await pcm_queue.put(None)

    async def _pcm_iter() -> AsyncIterator[bytes]:
        while True:
            chunk = await pcm_queue.get()
            if chunk is None:
                return
            yield chunk

    synthesize_task = asyncio.create_task(_synth_task(), name="tts-main-synth")
    playback_task = asyncio.create_task(audio.playback(_pcm_iter()), name="tts-main-playback")
    try:
        await asyncio.wait(
            {synthesize_task, playback_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in (synthesize_task, playback_task):
            if t.done() and not t.cancelled():
                exc = t.exception()
                if exc is not None:
                    raise exc
        # If one finished cleanly, wait for the other so playback
        # runs to completion after synthesis ends.
        for t in (synthesize_task, playback_task):
            if not t.done():
                await t
        print("[tts] OK", file=sys.stderr)
    finally:
        for t in (synthesize_task, playback_task):
            if not t.done():
                t.cancel()
            with contextlib.suppress(asyncio.CancelledError, BaseException):
                await t


if __name__ == "__main__":  # pragma: no cover
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        sys.exit(0)
