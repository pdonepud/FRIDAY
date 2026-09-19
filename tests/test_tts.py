"""Tests for the ElevenLabs streaming TTS module (``agent.tts``).

No test opens a real WebSocket. ``websockets.asyncio.client.connect`` is
mocked via ``monkeypatch`` — a fake exposes ``send()`` and async-iterates
raw JSON strings (test builds them with ``json.dumps``; the module under
test does ``json.loads``). This matches the receive shape ``synthesize``
uses on the raw websocket.

Manual hardware verification lives in ``python -m agent.tts``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import AsyncIterator
from inspect import isasyncgenfunction
from unittest.mock import MagicMock

import pytest
from elevenlabs.core.api_error import ApiError

import agent.tts
from agent.tts import (
    INACTIVITY_TIMEOUT_S,
    MODEL_ID,
    OUTPUT_FORMAT,
    PCM_QUEUE_MAX,
    VOICE_ID,
    TTSAuthError,
    TTSError,
    synthesize,
)

# ---------------------------------------------------------------------------
# Fake infrastructure — matches the mocking rigor of #48/#49/#50/#51.
# ---------------------------------------------------------------------------


def _audio_frame(payload: bytes, *, is_final: bool = False) -> str:
    """Build a raw JSON string for an audio frame with base64 payload."""
    frame: dict = {"audio": base64.b64encode(payload).decode("ascii")}
    if is_final:
        frame["isFinal"] = True
    return json.dumps(frame)


def _alignment_only_frame() -> str:
    """Build a raw JSON string for an alignment-only frame (no audio, no isFinal)."""
    return json.dumps(
        {
            "alignment": {"chars": ["h", "i"], "charStartTimesMs": [0, 100]},
            "normalizedAlignment": {"chars": ["h", "i"], "charStartTimesMs": [0, 100]},
        }
    )


def _error_frame(message: str) -> str:
    """Build a raw JSON string for a server error frame."""
    return json.dumps({"error": message})


def _final_frame(payload: bytes | None = None) -> str:
    """Build a raw JSON string for the final frame."""
    frame: dict = {"isFinal": True}
    if payload is not None:
        frame["audio"] = base64.b64encode(payload).decode("ascii")
    return json.dumps(frame)


class _FakeWebSocket:
    """Test double for ``websockets.asyncio.client`` connection.

    Async-iterable over a scripted sequence of raw JSON strings. Each
    entry may also be a ``BaseException`` instance to raise at that
    position. Records ``send()`` calls into ``sent_frames``.

    An optional ``post_end_of_input`` list is delivered only after the
    module under test sends its end-of-input frame (``{"text": ""}``),
    so tests can script "server responds only after we flush."
    """

    def __init__(
        self,
        pre_messages: list | None = None,
        post_end_of_input: list | None = None,
    ):
        self._pre = list(pre_messages or [])
        self._post = list(post_end_of_input or [])
        self._end_of_input_seen = asyncio.Event()
        self._hang = asyncio.Event()
        self.sent_frames: list[str] = []
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent_frames.append(data)
        # Detect the end-of-input signal ({"text": ""}) so we can
        # release the post-flush script.
        try:
            parsed = json.loads(data)
        except Exception:
            parsed = {}
        if parsed.get("text") == "":
            self._end_of_input_seen.set()

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for m in self._pre:
            if isinstance(m, BaseException):
                raise m
            yield m
        if self._post:
            await self._end_of_input_seen.wait()
            for m in self._post:
                if isinstance(m, BaseException):
                    raise m
                yield m
        # Otherwise block until cancelled.
        await self._hang.wait()


class _FakeConnectCM:
    """Async context manager yielding a ``_FakeWebSocket``."""

    def __init__(self, websocket: _FakeWebSocket, url: str, headers: dict):
        self.websocket = websocket
        self.url = url
        self.headers = headers
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> _FakeWebSocket:
        self.entered = True
        return self.websocket

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.exited = True
        self.websocket.closed = True
        return None


def _install_fake_ws(monkeypatch, websocket: _FakeWebSocket) -> list[_FakeConnectCM]:
    """Wire ``agent.tts.ws_connect`` to yield ``websocket`` in a fake CM.

    Returns a list that receives the created CM(s) so tests can assert
    on entered / exited / url / headers.
    """
    cms: list[_FakeConnectCM] = []

    def _connect(url: str, *, additional_headers: dict, **kwargs):
        cm = _FakeConnectCM(websocket, url, additional_headers)
        cms.append(cm)
        return cm

    monkeypatch.setattr(agent.tts, "ws_connect", _connect)
    return cms


def _install_key(monkeypatch, value: str = "sk_test") -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", value)


async def _chunks_from(items: list[str]) -> AsyncIterator[str]:
    for c in items:
        yield c


async def _empty_chunks() -> AsyncIterator[str]:
    if False:  # pragma: no cover
        yield ""


# ---------------------------------------------------------------------------
# Bundle 1: constants
# ---------------------------------------------------------------------------


def test_config_constants():
    assert VOICE_ID == "pFZP5JQG7iQjIQuC4Bku"
    assert MODEL_ID == "eleven_turbo_v2_5"
    assert OUTPUT_FORMAT == "pcm_24000"
    assert INACTIVITY_TIMEOUT_S == 25
    assert PCM_QUEUE_MAX == 96


# ---------------------------------------------------------------------------
# Bundle 2-3: lazy client construction + caching
# ---------------------------------------------------------------------------


def test_get_client_is_lazy(monkeypatch):
    monkeypatch.setattr(agent.tts, "_client", None)
    assert agent.tts._client is None


def test_get_client_caches_instance(monkeypatch):
    monkeypatch.setattr(agent.tts, "_client", None)
    fake_client = MagicMock(name="AsyncElevenLabs()")
    fake_ctor = MagicMock(return_value=fake_client)
    monkeypatch.setattr(agent.tts, "AsyncElevenLabs", fake_ctor)

    first = agent.tts._get_client()
    second = agent.tts._get_client()

    assert first is second is fake_client
    fake_ctor.assert_called_once()


# ---------------------------------------------------------------------------
# Bundle 4: API shape
# ---------------------------------------------------------------------------


def test_synthesize_is_async_generator_function():
    assert isasyncgenfunction(synthesize)


# ---------------------------------------------------------------------------
# Bundle 5: happy path — audio yielded in order
# ---------------------------------------------------------------------------


async def test_happy_path_yields_pcm_in_order(monkeypatch):
    _install_key(monkeypatch)
    ws = _FakeWebSocket(
        post_end_of_input=[
            _audio_frame(b"AAAA"),
            _audio_frame(b"BBBB"),
            _final_frame(payload=b"CCCC"),
        ]
    )
    _install_fake_ws(monkeypatch, ws)

    got = [pcm async for pcm in synthesize(_chunks_from(["Hello world."]))]

    assert got == [b"AAAA", b"BBBB", b"CCCC"]


# ---------------------------------------------------------------------------
# Bundle 6: URL / auth / init frame contents
# ---------------------------------------------------------------------------


async def test_ws_url_query_params_and_headers(monkeypatch):
    _install_key(monkeypatch, "sk_specific")
    ws = _FakeWebSocket(post_end_of_input=[_final_frame(b"X")])
    cms = _install_fake_ws(monkeypatch, ws)

    async for _ in synthesize(_chunks_from(["Hi."])):
        pass

    assert len(cms) == 1
    cm = cms[0]
    assert VOICE_ID in cm.url
    assert f"model_id={MODEL_ID}" in cm.url
    assert f"output_format={OUTPUT_FORMAT}" in cm.url
    assert cm.headers == {"xi-api-key": "sk_specific"}


async def test_init_frame_is_first_sent(monkeypatch):
    _install_key(monkeypatch)
    ws = _FakeWebSocket(post_end_of_input=[_final_frame(b"X")])
    _install_fake_ws(monkeypatch, ws)

    async for _ in synthesize(_chunks_from(["Hi."])):
        pass

    assert ws.sent_frames, "no frames sent"
    init = json.loads(ws.sent_frames[0])
    assert init["text"] == " "
    assert init["generation_config"] == {"chunk_length_schedule": [50]}


# ---------------------------------------------------------------------------
# Bundle 7: mid-word chunk splits get word-boundary padding
# ---------------------------------------------------------------------------


async def test_midword_splits_recombined_with_word_boundaries(monkeypatch):
    _install_key(monkeypatch)
    ws = _FakeWebSocket(post_end_of_input=[_final_frame(b"X")])
    _install_fake_ws(monkeypatch, ws)

    async for _ in synthesize(_chunks_from(["Hel", "lo the", "re."])):
        pass

    text_frames = [
        json.loads(f) for f in ws.sent_frames if json.loads(f).get("text") not in (" ", "", None)
    ]
    combined = "".join(f["text"] for f in text_frames)
    # Every text frame the SDK sends ends in a space per the SDK's
    # own text_chunker; our _text_chunker matches. Combined stream
    # is the original input recomposed with trailing space.
    assert combined.strip() == "Hello there."
    for f in text_frames:
        assert f["text"].endswith(" "), f"frame text should end in space: {f['text']!r}"
        assert f["try_trigger_generation"] is True


# ---------------------------------------------------------------------------
# Bundle 8: send ordering — init → text frames → end-of-input
# ---------------------------------------------------------------------------


async def test_send_order_init_text_end(monkeypatch):
    _install_key(monkeypatch)
    ws = _FakeWebSocket(post_end_of_input=[_final_frame(b"X")])
    _install_fake_ws(monkeypatch, ws)

    async for _ in synthesize(_chunks_from(["Hello.", "World."])):
        pass

    kinds = []
    for raw in ws.sent_frames:
        parsed = json.loads(raw)
        if parsed.get("text") == " ":
            kinds.append("init")
        elif parsed.get("text") == "":
            kinds.append("end")
        else:
            kinds.append("text")

    assert kinds[0] == "init"
    assert kinds[-1] == "end"
    assert kinds.count("init") == 1
    assert kinds.count("end") == 1
    assert "text" in kinds
    # No end frame before any text frame.
    first_end = kinds.index("end")
    text_indices = [i for i, k in enumerate(kinds) if k == "text"]
    assert all(i < first_end for i in text_indices), f"{kinds=}"


# ---------------------------------------------------------------------------
# Bundle 9: consumer early exit cancels sender, closes ws
# ---------------------------------------------------------------------------


async def test_consumer_early_exit_closes_ws(monkeypatch):
    _install_key(monkeypatch)
    ws = _FakeWebSocket(
        post_end_of_input=[
            _audio_frame(b"AAAA"),
            _audio_frame(b"BBBB"),
            _final_frame(b"CCCC"),
        ]
    )
    cms = _install_fake_ws(monkeypatch, ws)

    gen = synthesize(_chunks_from(["Hello."]))
    async for _ in gen:
        break  # early exit after first chunk
    await gen.aclose()

    assert cms[0].exited is True
    assert ws.closed is True


# ---------------------------------------------------------------------------
# Bundle 10: text_chunks raises → wraps to TTSError with __cause__ identity
# ---------------------------------------------------------------------------


async def test_text_chunks_raises_wraps_with_cause_identity(monkeypatch):
    _install_key(monkeypatch)
    ws = _FakeWebSocket()
    _install_fake_ws(monkeypatch, ws)

    sentinel = ValueError("upstream exploded")

    async def _bad() -> AsyncIterator[str]:
        yield "hello"
        raise sentinel

    with pytest.raises(TTSError) as exc_info:
        async for _ in synthesize(_bad()):
            pass
    assert "text source failed" in str(exc_info.value)
    assert exc_info.value.__cause__ is sentinel


# ---------------------------------------------------------------------------
# Bundle 11: connection closed mid-stream → TTSError
# ---------------------------------------------------------------------------


async def test_connection_closed_mid_stream(monkeypatch):
    from websockets.exceptions import ConnectionClosed

    class _Closed(ConnectionClosed):
        def __init__(self):  # type: ignore[no-untyped-def]
            Exception.__init__(self, "simulated close")

    _install_key(monkeypatch)
    ws = _FakeWebSocket(
        post_end_of_input=[
            _audio_frame(b"AAAA"),
            _Closed(),
        ]
    )
    _install_fake_ws(monkeypatch, ws)

    with pytest.raises(TTSError) as exc_info:
        async for _ in synthesize(_chunks_from(["Hello."])):
            pass
    assert "connection closed" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Bundle 12: server error frame → TTSError with server text
# ---------------------------------------------------------------------------


async def test_server_error_frame_becomes_TTSError(monkeypatch):
    _install_key(monkeypatch)
    ws = _FakeWebSocket(post_end_of_input=[_error_frame("voice quota exceeded")])
    _install_fake_ws(monkeypatch, ws)

    with pytest.raises(TTSError) as exc_info:
        async for _ in synthesize(_chunks_from(["Hi."])):
            pass
    assert "voice quota exceeded" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Bundle 13: empty input → no ws connection opened
# ---------------------------------------------------------------------------


async def test_empty_input_does_not_open_ws(monkeypatch):
    _install_key(monkeypatch)
    ws = _FakeWebSocket()
    cms = _install_fake_ws(monkeypatch, ws)

    got = [pcm async for pcm in synthesize(_empty_chunks())]

    assert got == []
    assert cms == []  # ws_connect never called


# ---------------------------------------------------------------------------
# Bundle 14: whitespace-only input → no ws opened
# ---------------------------------------------------------------------------


async def test_whitespace_only_input_does_not_open_ws(monkeypatch):
    _install_key(monkeypatch)
    ws = _FakeWebSocket()
    cms = _install_fake_ws(monkeypatch, ws)

    got = [pcm async for pcm in synthesize(_chunks_from(["   ", "\n", "\t"]))]

    assert got == []
    assert cms == []


# ---------------------------------------------------------------------------
# Bundle 14b: leading whitespace + real text → ws opens
# ---------------------------------------------------------------------------


async def test_leading_whitespace_then_real_text_opens_ws(monkeypatch):
    _install_key(monkeypatch)
    ws = _FakeWebSocket(post_end_of_input=[_final_frame(b"AAAA")])
    cms = _install_fake_ws(monkeypatch, ws)

    got = [pcm async for pcm in synthesize(_chunks_from(["   ", "\n", "Hello."]))]

    assert got == [b"AAAA"]
    assert len(cms) == 1


# ---------------------------------------------------------------------------
# Bundle 15: odd-length audio chunk passes through unchanged
# ---------------------------------------------------------------------------


async def test_odd_length_audio_chunk_passes_through(monkeypatch):
    _install_key(monkeypatch)
    # 3 bytes: odd length; agent.audio.playback aligns downstream, we
    # must not truncate or pad here.
    ws = _FakeWebSocket(
        post_end_of_input=[
            _audio_frame(b"\x01\x02\x03"),
            _final_frame(b"\x04\x05"),
        ]
    )
    _install_fake_ws(monkeypatch, ws)

    got = [pcm async for pcm in synthesize(_chunks_from(["Hi."]))]

    assert got == [b"\x01\x02\x03", b"\x04\x05"]


# ---------------------------------------------------------------------------
# Bundle 16: inactivity watchdog — fires on true stall, reschedules on frames
# ---------------------------------------------------------------------------


async def test_inactivity_fires_on_true_stall(monkeypatch):
    """No frames at all → TTSError('no audio within Ns') within the budget."""
    _install_key(monkeypatch)
    monkeypatch.setattr(agent.tts, "INACTIVITY_TIMEOUT_S", 0.05)
    ws = _FakeWebSocket()  # never yields anything
    _install_fake_ws(monkeypatch, ws)

    with pytest.raises(TTSError, match="no audio within"):
        async for _ in synthesize(_chunks_from(["Hi."])):
            pass


class _PacedFakeWebSocket(_FakeWebSocket):
    """FakeWebSocket that inserts a delay between post-flush frames."""

    def __init__(
        self,
        pre_messages: list | None = None,
        post_end_of_input: list | None = None,
        gap_seconds: float = 0.0,
    ):
        super().__init__(pre_messages=pre_messages, post_end_of_input=post_end_of_input)
        self._gap = gap_seconds

    async def _iter(self):
        for m in self._pre:
            if isinstance(m, BaseException):
                raise m
            yield m
        if self._post:
            await self._end_of_input_seen.wait()
            for m in self._post:
                if self._gap > 0:
                    await asyncio.sleep(self._gap)
                if isinstance(m, BaseException):
                    raise m
                yield m
        await self._hang.wait()


async def test_inactivity_reschedules_across_long_stream(monkeypatch):
    """Frames arriving at 0.6× the timeout keep resetting deadline (a).

    Total wall-clock (7 gaps × 0.12s ≈ 0.84s) exceeds
    INACTIVITY_TIMEOUT_S (0.2s), but per-frame gaps stay under it.
    Without the (a) reset, the second or third frame would trigger a
    spurious timeout. Values are big enough to survive Windows event-
    loop scheduling jitter (~15ms in the worst case observed).
    """
    _install_key(monkeypatch)
    monkeypatch.setattr(agent.tts, "INACTIVITY_TIMEOUT_S", 0.2)

    payloads = [b"AAAA", b"BBBB", b"CCCC", b"DDDD", b"EEEE", b"FFFF", b"GGGG"]
    ws = _PacedFakeWebSocket(
        post_end_of_input=[
            *(_audio_frame(p) for p in payloads),
            _final_frame(b"ZZZZ"),
        ],
        gap_seconds=0.12,  # 60% of 0.2
    )
    _install_fake_ws(monkeypatch, ws)

    got = [pcm async for pcm in synthesize(_chunks_from(["Hi."]))]

    assert got == payloads + [b"ZZZZ"]


async def test_inactivity_reschedules_across_slow_playback(monkeypatch):
    """Consumer paces slower than the timeout; put() blocks trigger (b) reset.

    PCM_QUEUE_MAX=1 so each ``pcm_queue.put`` in the receiver blocks
    until the consumer pulls. Consumer's ``async for`` body sleeps
    0.12s — 60% larger than the 0.2s window minus the queue-drain
    overhead. Without the (b) reset — i.e., if the deadline stays at
    "frame-arrival + INACTIVITY_TIMEOUT_S" — the wait for the second
    frame after a slow put-return would fire the timeout spuriously.
    Values chosen to survive Windows event-loop jitter.
    """
    _install_key(monkeypatch)
    monkeypatch.setattr(agent.tts, "INACTIVITY_TIMEOUT_S", 0.2)
    monkeypatch.setattr(agent.tts, "PCM_QUEUE_MAX", 1)

    payloads = [b"AAAA", b"BBBB", b"CCCC"]
    ws = _FakeWebSocket(
        post_end_of_input=[
            *(_audio_frame(p) for p in payloads),
            _final_frame(b"DDDD"),
        ]
    )
    _install_fake_ws(monkeypatch, ws)

    got: list[bytes] = []
    async for pcm in synthesize(_chunks_from(["Hi."])):
        got.append(pcm)
        await asyncio.sleep(0.12)

    assert got == payloads + [b"DDDD"]


# ---------------------------------------------------------------------------
# Bundle 17: missing ELEVENLABS_API_KEY at entry → TTSAuthError
# ---------------------------------------------------------------------------


async def test_missing_api_key_raises_TTSAuthError(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    ws = _FakeWebSocket()
    cms = _install_fake_ws(monkeypatch, ws)

    with pytest.raises(TTSAuthError, match="ELEVENLABS_API_KEY missing"):
        async for _ in synthesize(_chunks_from(["Hi."])):
            pass
    # No ws opened either — auth check happens BEFORE peek/open.
    assert cms == []


async def test_empty_api_key_raises_TTSAuthError(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "")
    ws = _FakeWebSocket()
    _install_fake_ws(monkeypatch, ws)

    with pytest.raises(TTSAuthError):
        async for _ in synthesize(_chunks_from(["Hi."])):
            pass


# ---------------------------------------------------------------------------
# Bundle 18: 401 from server close → TTSAuthError
# ---------------------------------------------------------------------------


async def test_handshake_401_becomes_TTSAuthError(monkeypatch):
    from websockets.exceptions import ConnectionClosed

    _install_key(monkeypatch)

    class _Rcvd:
        code = 1008  # policy-violation close code the module maps to auth

    class _Closed(ConnectionClosed):
        def __init__(self):  # type: ignore[no-untyped-def]
            Exception.__init__(self, "auth rejected")
            self.rcvd = _Rcvd()

    class _BadCM:
        async def __aenter__(self):
            raise _Closed()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    def _connect(url, *, additional_headers, **kwargs):
        return _BadCM()

    monkeypatch.setattr(agent.tts, "ws_connect", _connect)

    with pytest.raises(TTSAuthError):
        async for _ in synthesize(_chunks_from(["Hi."])):
            pass


# ---------------------------------------------------------------------------
# Bundle 19: ApiError from client construction path → wrapped
# ---------------------------------------------------------------------------


async def test_api_error_401_becomes_TTSAuthError(monkeypatch):
    _install_key(monkeypatch)

    class _BadCM:
        async def __aenter__(self):
            raise ApiError(status_code=401, body="invalid")

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(agent.tts, "ws_connect", lambda url, *, additional_headers, **kw: _BadCM())

    with pytest.raises(TTSAuthError):
        async for _ in synthesize(_chunks_from(["Hi."])):
            pass


async def test_api_error_500_becomes_generic_TTSError(monkeypatch):
    _install_key(monkeypatch)

    class _BadCM:
        async def __aenter__(self):
            raise ApiError(status_code=500, body="server melted")

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(agent.tts, "ws_connect", lambda url, *, additional_headers, **kw: _BadCM())

    with pytest.raises(TTSError) as exc_info:
        async for _ in synthesize(_chunks_from(["Hi."])):
            pass
    assert type(exc_info.value) is TTSError
    assert "status=500" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Bundle 20: cancellation cleans up ws + tasks
# ---------------------------------------------------------------------------


async def test_cancellation_closes_ws(monkeypatch):
    _install_key(monkeypatch)
    ws = _FakeWebSocket()  # hangs; no messages
    cms = _install_fake_ws(monkeypatch, ws)

    async def _run():
        async for _ in synthesize(_chunks_from(["Hi."])):
            pass

    task = asyncio.create_task(_run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cms[0].exited is True
    assert ws.closed is True


# ---------------------------------------------------------------------------
# Bundle 21: _main dispatch wiring
# ---------------------------------------------------------------------------


async def test_main_wires_synthesize_and_playback(monkeypatch, capsys):
    """_main pipes synthesize into agent.audio.playback."""
    import agent.audio as _audio_mod

    played: list[bytes] = []

    async def _fake_synthesize(text_chunks):
        # Consume the input so the sender wouldn't hang if this were real.
        async for _ in text_chunks:
            pass
        yield b"AAAA"
        yield b"BBBB"

    async def _fake_playback(chunks):
        async for c in chunks:
            played.append(c)

    monkeypatch.setattr(agent.tts, "synthesize", _fake_synthesize)
    monkeypatch.setattr(_audio_mod, "playback", _fake_playback)

    await agent.tts._main()

    assert played == [b"AAAA", b"BBBB"]
    captured = capsys.readouterr()
    assert "OK" in captured.err


# ---------------------------------------------------------------------------
# Bundle 22: text_chunks raises → __cause__ identity preserved
# (This is bundle 10 restated as an explicit identity check for the
# Tightening 3 discipline. Kept separate so a regression is easy to
# see even if bundle 10 gets modified.)
# ---------------------------------------------------------------------------


async def test_text_chunks_cause_identity(monkeypatch):
    _install_key(monkeypatch)
    ws = _FakeWebSocket()
    _install_fake_ws(monkeypatch, ws)

    sentinel = RuntimeError("chunk source blew up")

    async def _bad() -> AsyncIterator[str]:
        yield "hello there."
        raise sentinel

    with pytest.raises(TTSError) as exc_info:
        async for _ in synthesize(_bad()):
            pass
    # Identity, not just isinstance.
    assert exc_info.value.__cause__ is sentinel


# ---------------------------------------------------------------------------
# Bundle 23: sender-death detection — deterministic, well under timeout
# ---------------------------------------------------------------------------


async def test_sender_death_terminates_fast(monkeypatch):
    """text_chunks raises → synthesize exits within a wall-clock ceiling
    well below INACTIVITY_TIMEOUT_S. Proves the sender-death signal path
    (pcm_queue.put(None) from sender's except-block), not the timeout
    fallback. INACTIVITY_TIMEOUT_S is bumped to 60s so a broken code path
    would deterministically take >>0.5s.
    """
    _install_key(monkeypatch)
    monkeypatch.setattr(agent.tts, "INACTIVITY_TIMEOUT_S", 60)
    ws = _FakeWebSocket()  # never yields anything
    _install_fake_ws(monkeypatch, ws)

    async def _bad() -> AsyncIterator[str]:
        yield "hello."
        raise ValueError("boom")

    start = time.monotonic()
    with pytest.raises(TTSError):
        async for _ in synthesize(_bad()):
            pass
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, f"sender-death took {elapsed:.3f}s — timeout fallback fired"


# ---------------------------------------------------------------------------
# Bundle 24: alignment-only frame between audio frames — ignored, order kept
# ---------------------------------------------------------------------------


async def test_alignment_only_frame_ignored(monkeypatch):
    _install_key(monkeypatch)
    ws = _FakeWebSocket(
        post_end_of_input=[
            _audio_frame(b"AAAA"),
            _alignment_only_frame(),
            _final_frame(b"BBBB"),
        ]
    )
    _install_fake_ws(monkeypatch, ws)

    got = [pcm async for pcm in synthesize(_chunks_from(["Hi."]))]

    assert got == [b"AAAA", b"BBBB"]


# ---------------------------------------------------------------------------
# Bundle 26: text_chunker — splitter-at-start of a chunk
# ---------------------------------------------------------------------------


async def test_text_chunker_splitter_at_chunk_start(monkeypatch):
    """Chunk whose first char is a splitter absorbs one char into the flush."""
    _install_key(monkeypatch)
    ws = _FakeWebSocket(post_end_of_input=[_final_frame(b"X")])
    _install_fake_ws(monkeypatch, ws)

    async for _ in synthesize(_chunks_from(["Hel", ".lo world."])):
        pass

    text_frames = [
        json.loads(f)["text"]
        for f in ws.sent_frames
        if json.loads(f).get("text") not in (" ", "", None)
    ]
    # First flush: "Hel" + "." → "Hel. " (splitter-at-start branch).
    assert any(t.startswith("Hel.") for t in text_frames), (
        f"expected splitter-at-start flush 'Hel. ', got frames {text_frames!r}"
    )


# ---------------------------------------------------------------------------
# Bundle 27: OSError at connect → generic TTSError
# ---------------------------------------------------------------------------


async def test_oserror_at_connect_becomes_TTSError(monkeypatch):
    _install_key(monkeypatch)

    class _BadCM:
        async def __aenter__(self):
            raise OSError("no route to host")

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(agent.tts, "ws_connect", lambda url, *, additional_headers, **kw: _BadCM())

    with pytest.raises(TTSError) as exc_info:
        async for _ in synthesize(_chunks_from(["Hi."])):
            pass
    assert type(exc_info.value) is TTSError
    assert "connection failed" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, OSError)


# ---------------------------------------------------------------------------
# Bundle 28: WebSocketException (non-ConnectionClosed) at connect → TTSError
# ---------------------------------------------------------------------------


async def test_websocket_exception_at_connect_becomes_TTSError(monkeypatch):
    from websockets.exceptions import InvalidURI, WebSocketException

    assert issubclass(InvalidURI, WebSocketException)

    _install_key(monkeypatch)

    class _BadCM:
        async def __aenter__(self):
            raise InvalidURI("bad://url", "malformed")

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(agent.tts, "ws_connect", lambda url, *, additional_headers, **kw: _BadCM())

    with pytest.raises(TTSError) as exc_info:
        async for _ in synthesize(_chunks_from(["Hi."])):
            pass
    assert "connection failed" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Bundle 29: _wrap_handshake_close with unknown code → generic TTSError
# ---------------------------------------------------------------------------


async def test_handshake_close_unknown_code_generic_TTSError(monkeypatch):
    from websockets.exceptions import ConnectionClosed

    _install_key(monkeypatch)

    class _Rcvd:
        code = 1011  # server error, not an auth-signalling code we recognize

    class _Closed(ConnectionClosed):
        def __init__(self):  # type: ignore[no-untyped-def]
            Exception.__init__(self, "generic close")
            self.rcvd = _Rcvd()

    class _BadCM:
        async def __aenter__(self):
            raise _Closed()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(agent.tts, "ws_connect", lambda url, *, additional_headers, **kw: _BadCM())

    with pytest.raises(TTSError) as exc_info:
        async for _ in synthesize(_chunks_from(["Hi."])):
            pass
    # NOT the auth subclass — unknown close code falls through.
    assert type(exc_info.value) is TTSError
    assert not isinstance(exc_info.value, TTSAuthError)
    assert "1011" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Bundle 25: pipeline error suppressed when caller unwinds an exception
# (Concern 1 from Preetam's tightening — sys.exc_info gating.)
# ---------------------------------------------------------------------------


async def test_pipeline_error_suppressed_on_caller_unwind(monkeypatch, caplog):
    """When the consumer raises inside its ``async for`` body, an
    in-flight pipeline error must NOT mask the caller's exception.
    """
    caplog.set_level(logging.WARNING, logger="agent.tts")
    _install_key(monkeypatch)

    # Server sends an error frame right after we start yielding — but
    # the consumer raises before that error surfaces.
    ws = _FakeWebSocket(
        post_end_of_input=[
            _audio_frame(b"AAAA"),
            _error_frame("late server error"),
        ]
    )
    _install_fake_ws(monkeypatch, ws)

    class _ConsumerErr(Exception):
        pass

    with pytest.raises(_ConsumerErr):
        async for _ in synthesize(_chunks_from(["Hi."])):
            raise _ConsumerErr("consumer bailed")

    # The consumer's exception surfaces, NOT the server error frame.
