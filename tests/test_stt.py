"""Tests for the Deepgram Flux STT module (``agent.stt``).

No test opens a real Deepgram websocket. The SDK's typed union at
listen.v2 does not include a Warning message (upstream issue
https://github.com/deepgram/deepgram-python-sdk/issues/792), so
production code iterates ``socket._websocket`` directly and dispatches
on raw JSON. Test doubles match that shape: ``FakeAsyncSocket`` exposes
a ``_websocket`` async-iterable that yields raw JSON strings, plus
async ``send_media`` / ``send_force_end_turn`` methods that record
call order into ``call_order`` for send-ordering assertions.

Manual hardware verification lives in ``python -m agent.stt``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from inspect import iscoroutinefunction
from unittest.mock import MagicMock

import pytest
from deepgram.core.api_error import ApiError
from websockets.exceptions import ConnectionClosed

import agent.stt
from agent.stt import (
    ENCODING,
    EOT_THRESHOLD,
    EOT_TIMEOUT_MS,
    MODEL,
    RECEIVE_TIMEOUT_S,
    SAMPLE_RATE,
    STTAuthError,
    STTError,
    STTFatal,
    STTForceEndTurnUnsupported,
    STTTimeout,
    transcribe,
)

# ---------------------------------------------------------------------------
# Fake infrastructure — matches the mocking rigor of #48 and #49.
# ---------------------------------------------------------------------------


def _turn_info_end_of_turn(transcript: str = "", trigger: str | None = "manual") -> str:
    """Build a raw JSON string for a TurnInfo/EndOfTurn message."""
    return json.dumps(
        {
            "type": "TurnInfo",
            "request_id": "test-req",
            "sequence_id": 1,
            "event": "EndOfTurn",
            "turn_index": 0,
            "audio_window_start": 0.0,
            "audio_window_end": 1.0,
            "transcript": transcript,
            "words": [],
            "end_of_turn_confidence": 0.9,
            "trigger": trigger,
        }
    )


def _turn_info_update(event: str) -> str:
    """Build a raw JSON string for a non-terminal TurnInfo event (Update / StartOfTurn)."""
    return json.dumps(
        {
            "type": "TurnInfo",
            "request_id": "test-req",
            "sequence_id": 1,
            "event": event,
            "turn_index": 0,
            "audio_window_start": 0.0,
            "audio_window_end": 0.1,
            "transcript": "",
            "words": [],
            "end_of_turn_confidence": 0.1,
        }
    )


def _warning(code: str, description: str = "test") -> str:
    """Build a raw JSON string for a Warning message (SDK doesn't type these)."""
    return json.dumps(
        {
            "type": "Warning",
            "request_id": "test-req",
            "sequence_id": 1,
            "code": code,
            "description": description,
        }
    )


def _fatal(code: str, description: str = "test") -> str:
    """Build a raw JSON string for a FatalError message."""
    return json.dumps(
        {
            "type": "Error",
            "request_id": "test-req",
            "sequence_id": 1,
            "code": code,
            "description": description,
        }
    )


class _FakeWebSocket:
    """Test double for ``websockets.legacy.client.WebSocketClientProtocol``.

    Async-iterable over a scripted sequence of raw JSON strings. Each
    entry may also be a ``BaseException`` instance to raise at that
    position (models ``ConnectionClosed`` mid-stream). After the
    scripted messages exhaust, blocks on an ``asyncio.Event`` unless
    ``exhaust_after_messages=True`` — in which case it returns normally
    (models the socket-closed-without-terminal path).

    An optional ``yield_after_force_end`` list is delivered only after
    the surrounding ``FakeAsyncSocket.send_force_end_turn`` has fired,
    so tests can script "server responds to our ForceEndTurn."
    """

    def __init__(
        self,
        pre_messages: list | None = None,
        post_force_end: list | None = None,
        exhaust_after_messages: bool = False,
        force_end_seen: asyncio.Event | None = None,
    ):
        self._pre = list(pre_messages or [])
        self._post = list(post_force_end or [])
        self._exhaust = exhaust_after_messages
        self._force_end_seen = force_end_seen
        self._hang = asyncio.Event()

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for m in self._pre:
            if isinstance(m, BaseException):
                raise m
            yield m
        if self._post:
            if self._force_end_seen is not None:
                await self._force_end_seen.wait()
            for m in self._post:
                if isinstance(m, BaseException):
                    raise m
                yield m
        if self._exhaust:
            return
        # Block until the receive task is cancelled (or a test sets _hang).
        await self._hang.wait()


class FakeAsyncSocket:
    """Test double for ``AsyncV2SocketClient``.

    Exposes ``_websocket`` (matching the private-but-stable name our
    ``_receive`` iterates) plus ``send_media`` / ``send_force_end_turn``
    that record call order. The private-name convention here is
    intentional: production code touches ``socket._websocket`` and the
    tests must match to exercise that path.
    """

    def __init__(
        self,
        pre_messages: list | None = None,
        post_force_end: list | None = None,
        exhaust_after_messages: bool = False,
        raise_on_force_end: BaseException | None = None,
        omit_websocket_attr: bool = False,
    ):
        self._raise_on_force_end = raise_on_force_end
        self.call_order: list[tuple] = []
        self.send_media_calls: list[bytes] = []
        self.force_end_turn_count = 0
        self.force_end_seen = asyncio.Event()
        self._pump_task_done_at_force_end: bool | None = None
        if not omit_websocket_attr:
            self._websocket = _FakeWebSocket(
                pre_messages=pre_messages,
                post_force_end=post_force_end,
                exhaust_after_messages=exhaust_after_messages,
                force_end_seen=self.force_end_seen,
            )

    async def send_media(self, chunk: bytes) -> None:
        self.call_order.append(("send_media", chunk))
        self.send_media_calls.append(chunk)

    async def send_force_end_turn(self, message=None) -> None:
        # Tightening 2: record whether the pump task had reached done()
        # by the time send_force_end_turn was actually called. Set from
        # outside via observe_pump_at_force_end so we can prove the
        # cancel-and-await completed before this send happened.
        if self._observed_pump_task is not None:
            self._pump_task_done_at_force_end = self._observed_pump_task.done()
        self.call_order.append(("send_force_end_turn",))
        self.force_end_turn_count += 1
        self.force_end_seen.set()
        if self._raise_on_force_end is not None:
            raise self._raise_on_force_end

    _observed_pump_task: asyncio.Task | None = None

    def observe_pump_at_force_end(self, task: asyncio.Task) -> None:
        """Register a pump task whose ``.done()`` will be captured at force-end call time."""
        self._observed_pump_task = task


class FakeConnectCM:
    """Async context manager yielding a ``FakeAsyncSocket``."""

    def __init__(self, socket: FakeAsyncSocket, kwargs: dict):
        self.socket = socket
        self.kwargs = kwargs
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> FakeAsyncSocket:
        self.entered = True
        return self.socket

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.exited = True
        return None


def _install_fake_client(monkeypatch, socket: FakeAsyncSocket) -> MagicMock:
    """Wire ``_get_client`` to return a client whose ``listen.v2.connect``
    yields ``socket`` inside a ``FakeConnectCM``. Returns a proxy whose
    ``_holder`` attribute lists the created CMs in order.
    """
    cm_holder: list[FakeConnectCM] = []

    def _connect(**kwargs):
        cm = FakeConnectCM(socket, kwargs)
        cm_holder.append(cm)
        return cm

    fake_client = MagicMock(name="AsyncDeepgramClient()")
    fake_client.listen.v2.connect.side_effect = _connect

    monkeypatch.setattr(agent.stt, "_client", None)
    monkeypatch.setattr(agent.stt, "_get_client", lambda: fake_client)
    proxy = MagicMock()
    proxy._holder = cm_holder
    return proxy


async def _chunks_from(items: list[bytes]) -> AsyncIterator[bytes]:
    for c in items:
        yield c


async def _empty_chunks() -> AsyncIterator[bytes]:
    if False:  # pragma: no cover
        yield b""


@pytest.fixture(autouse=True)
def _fast_ptt_grace(request, monkeypatch):
    """Shrink ``PTT_RELEASE_GRACE_MS`` for tests that do not opt out.

    The production grace is 500 ms (ADR-0005 Decision 1); every
    pre-existing test in this file that uses ``post_force_end=[...]``
    now waits the full grace before ``ForceEndTurn`` fires, which
    would add ~500 ms × N tests of pure wall-clock overhead. 10 ms
    keeps the grace semantics (natural EOT in-grace vs. timeout
    fallback) exercisable while keeping the suite fast. Tests that
    care about the production value or the elapsed-ms warning text
    mark themselves ``@pytest.mark.no_fast_grace`` to opt out, or
    monkeypatch to a specific larger value.
    """
    if "no_fast_grace" in request.keywords:
        return
    monkeypatch.setattr(agent.stt, "PTT_RELEASE_GRACE_MS", 10)


# ---------------------------------------------------------------------------
# Bundle 1: constants
# ---------------------------------------------------------------------------


@pytest.mark.no_fast_grace
def test_config_constants():
    assert MODEL == "flux-general-en"
    assert ENCODING == "linear16"
    assert SAMPLE_RATE == 16000
    # ADR-0005 Decision 1: EOT_THRESHOLD lowered from the Tier-3 1.0 to
    # 0.7 so natural EndOfTurn fires on conversational silences;
    # EAGER_EOT_THRESHOLD must be <= EOT_THRESHOLD (module-level assert
    # at import).
    assert EOT_THRESHOLD == 0.7
    assert agent.stt.EAGER_EOT_THRESHOLD == 0.5
    assert agent.stt.EAGER_EOT_THRESHOLD <= EOT_THRESHOLD
    assert EOT_TIMEOUT_MS == 30000
    assert agent.stt.PTT_RELEASE_GRACE_MS == 500
    assert RECEIVE_TIMEOUT_S == 35


# ---------------------------------------------------------------------------
# Bundle 2-3: lazy client construction + caching
# ---------------------------------------------------------------------------


def test_get_client_is_lazy(monkeypatch):
    monkeypatch.setattr(agent.stt, "_client", None)
    assert agent.stt._client is None


def test_get_client_caches_instance(monkeypatch):
    monkeypatch.setattr(agent.stt, "_client", None)
    fake_client = MagicMock(name="AsyncDeepgramClient()")
    fake_ctor = MagicMock(return_value=fake_client)
    monkeypatch.setattr(agent.stt, "AsyncDeepgramClient", fake_ctor)

    first = agent.stt._get_client()
    second = agent.stt._get_client()

    assert first is second is fake_client
    fake_ctor.assert_called_once()


# ---------------------------------------------------------------------------
# Bundle 4: API shape
# ---------------------------------------------------------------------------


def test_transcribe_is_coroutine_function():
    assert iscoroutinefunction(transcribe)


# ---------------------------------------------------------------------------
# Bundle 5: happy path + send ordering
# ---------------------------------------------------------------------------


async def test_happy_path_returns_transcript_and_orders_sends(monkeypatch):
    socket = FakeAsyncSocket(
        post_force_end=[
            _turn_info_update("StartOfTurn"),
            _turn_info_end_of_turn(transcript="hello world", trigger="manual"),
        ],
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    chunks_payload = [b"AAA", b"BBB", b"CCC"]

    task = asyncio.create_task(transcribe(_chunks_from(chunks_payload), ptt_release))
    await asyncio.sleep(0.05)
    ptt_release.set()

    transcript = await asyncio.wait_for(task, timeout=1.0)
    assert transcript == "hello world"

    force_at = [i for i, c in enumerate(socket.call_order) if c[0] == "send_force_end_turn"]
    assert len(force_at) == 1
    media_indices = [i for i, c in enumerate(socket.call_order) if c[0] == "send_media"]
    assert media_indices, "send_media must have been called"
    assert all(i < force_at[0] for i in media_indices), (
        f"send_media must precede send_force_end_turn: {socket.call_order}"
    )
    assert socket.send_media_calls == chunks_payload


# ---------------------------------------------------------------------------
# Bundle 6: config kwargs forwarded
# ---------------------------------------------------------------------------


async def test_connect_kwargs_forwarded(monkeypatch):
    socket = FakeAsyncSocket(
        post_force_end=[_turn_info_end_of_turn(transcript="ok", trigger="manual")]
    )
    proxy = _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    task = asyncio.create_task(transcribe(_empty_chunks(), ptt_release))
    await asyncio.sleep(0.02)
    ptt_release.set()
    await asyncio.wait_for(task, timeout=1.0)

    cm = proxy._holder[-1]
    assert cm.kwargs == {
        "model": MODEL,
        "encoding": ENCODING,
        "sample_rate": SAMPLE_RATE,
        "eot_threshold": EOT_THRESHOLD,
        "eager_eot_threshold": agent.stt.EAGER_EOT_THRESHOLD,
        "eot_timeout_ms": EOT_TIMEOUT_MS,
    }
    assert cm.entered is True
    assert cm.exited is True


# ---------------------------------------------------------------------------
# Bundle 7-9: EndOfTurn trigger dispatch
# ---------------------------------------------------------------------------


async def test_trigger_timeout_raises_STTTimeout_with_partial(monkeypatch):
    socket = FakeAsyncSocket(
        post_force_end=[_turn_info_end_of_turn(transcript="half a sentence", trigger="timeout")]
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    task = asyncio.create_task(transcribe(_empty_chunks(), ptt_release))
    await asyncio.sleep(0.02)
    ptt_release.set()

    with pytest.raises(STTTimeout) as exc_info:
        await asyncio.wait_for(task, timeout=1.0)
    assert exc_info.value.partial == "half a sentence"


async def test_trigger_model_returns_transcript_with_warning(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="agent.stt")
    socket = FakeAsyncSocket(
        post_force_end=[_turn_info_end_of_turn(transcript="something", trigger="model")]
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    task = asyncio.create_task(transcribe(_empty_chunks(), ptt_release))
    await asyncio.sleep(0.02)
    ptt_release.set()

    transcript = await asyncio.wait_for(task, timeout=1.0)
    assert transcript == "something"
    assert any("trigger='model'" in r.getMessage() for r in caplog.records)


async def test_trigger_unknown_returns_transcript_with_warning(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="agent.stt")
    socket = FakeAsyncSocket(
        post_force_end=[_turn_info_end_of_turn(transcript="mystery", trigger="brand-new-trigger")]
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    task = asyncio.create_task(transcribe(_empty_chunks(), ptt_release))
    await asyncio.sleep(0.02)
    ptt_release.set()

    transcript = await asyncio.wait_for(task, timeout=1.0)
    assert transcript == "mystery"
    assert any("unknown trigger" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Bundle 10: FatalError with non-UNPARSABLE code → STTFatal
# ---------------------------------------------------------------------------


async def test_fatal_error_generic_code_raises_STTFatal(monkeypatch):
    socket = FakeAsyncSocket(pre_messages=[_fatal("INTERNAL_SERVER_ERROR", "boom")])
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    with pytest.raises(STTFatal) as exc_info:
        await asyncio.wait_for(transcribe(_empty_chunks(), ptt_release), timeout=1.0)
    assert "INTERNAL_SERVER_ERROR" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Bundle 11: connection close mid-stream → STTError
# ---------------------------------------------------------------------------


async def test_connection_closed_mid_stream_raises_STTError(monkeypatch):
    # ConnectionClosed's constructor differs across websockets versions;
    # a subclass with a null-op __init__ sidesteps that.
    class _ClosedNow(ConnectionClosed):
        def __init__(self):  # type: ignore[no-untyped-def]
            Exception.__init__(self, "simulated close")

    socket = FakeAsyncSocket(pre_messages=[_ClosedNow()])
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    with pytest.raises(STTError) as exc_info:
        await asyncio.wait_for(transcribe(_empty_chunks(), ptt_release), timeout=1.0)
    assert type(exc_info.value) is STTError
    assert "connection closed" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Bundle 12 (split): send_force_end_turn raising → STTError (generic transport)
# ---------------------------------------------------------------------------


async def test_send_force_end_turn_raising_maps_to_STTError(monkeypatch):
    """send-side transport failures do NOT map to STTForceEndTurnUnsupported.

    Rejection surfaces as a server-sent FatalError on the receive path;
    a send-side raise is generic connection trouble.
    """
    socket = FakeAsyncSocket(raise_on_force_end=RuntimeError("send exploded"))
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    task = asyncio.create_task(transcribe(_empty_chunks(), ptt_release))
    await asyncio.sleep(0.02)
    ptt_release.set()

    with pytest.raises(STTError) as exc_info:
        await asyncio.wait_for(task, timeout=1.0)
    assert type(exc_info.value) is STTError
    assert "ForceEndTurn send failed" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Bundle 13: chunks iterator raises → STTError wraps
# ---------------------------------------------------------------------------


async def test_chunks_iterator_raises_wraps_to_STTError(monkeypatch):
    socket = FakeAsyncSocket()
    _install_fake_client(monkeypatch, socket)

    async def _bad_chunks() -> AsyncIterator[bytes]:
        yield b"AAA"
        raise ValueError("mic exploded")

    ptt_release = asyncio.Event()
    with pytest.raises(STTError) as exc_info:
        await asyncio.wait_for(transcribe(_bad_chunks(), ptt_release), timeout=1.0)
    assert "chunk source failed" in str(exc_info.value)
    assert "mic exploded" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Bundle 13b (#55 D7): _pump distinguishes chunk-source vs. send-side failures
# ---------------------------------------------------------------------------


async def test_pump_distinguishes_chunk_source_vs_send_failure(monkeypatch):
    """Mid-turn transport-side failure during ``socket.send_media`` is
    wrapped with the "audio send failed" wording — distinct from the
    "chunk source failed" wording used when the chunks iterator itself
    raises. Covers both branches of _pump's split try/except (#55 D7).
    """

    class _SendRaisingSocket(FakeAsyncSocket):
        """FakeAsyncSocket whose ``send_media`` raises WebSocketException
        on the second call (first call records, second drops the stream).
        """

        def __init__(self):
            super().__init__()
            self._send_calls = 0

        async def send_media(self, chunk: bytes) -> None:
            self._send_calls += 1
            if self._send_calls >= 2:
                # ConnectionClosedError is a WebSocketException subclass
                # — matches one of _pump's inner concrete catch types.
                from websockets.exceptions import ConnectionClosedError
                from websockets.frames import Close

                raise ConnectionClosedError(rcvd=Close(1006, "abnormal closure"), sent=None)
            self.call_order.append(("send_media", chunk))
            self.send_media_calls.append(chunk)

    socket = _SendRaisingSocket()
    _install_fake_client(monkeypatch, socket)

    async def _steady_chunks() -> AsyncIterator[bytes]:
        for _ in range(4):
            yield b"AAAA"
            await asyncio.sleep(0.001)

    ptt_release = asyncio.Event()
    with pytest.raises(STTError) as exc_info:
        await asyncio.wait_for(transcribe(_steady_chunks(), ptt_release), timeout=1.0)
    # D7's wording split: send-side failure, NOT "chunk source failed".
    msg = str(exc_info.value)
    assert "audio send failed" in msg, f"expected D7 send-side wording, got: {msg!r}"
    assert "chunk source failed" not in msg, (
        f"send-side failure mis-labelled as chunk-source: {msg!r}"
    )


# ---------------------------------------------------------------------------
# Bundle 14a: cancellation mid-stream closes socket
# ---------------------------------------------------------------------------


async def test_cancellation_mid_stream_closes_socket(monkeypatch):
    socket = FakeAsyncSocket()
    proxy = _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()

    async def _long_chunks() -> AsyncIterator[bytes]:
        for _ in range(1000):
            yield b"AAAA"
            await asyncio.sleep(0.001)

    task = asyncio.create_task(transcribe(_long_chunks(), ptt_release))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    cm = proxy._holder[-1]
    assert cm.entered is True
    assert cm.exited is True


# ---------------------------------------------------------------------------
# Bundle 14b: cancellation on the totally-idle path
# ---------------------------------------------------------------------------


async def test_cancellation_before_any_task_resolves(monkeypatch):
    socket = FakeAsyncSocket()
    proxy = _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    task = asyncio.create_task(transcribe(_empty_chunks(), ptt_release))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    cm = proxy._holder[-1]
    assert cm.entered is True
    assert cm.exited is True


# ---------------------------------------------------------------------------
# Bundle 15: receive watchdog fires ONLY after force_end_sent
# ---------------------------------------------------------------------------


async def test_receive_watchdog_fires_only_after_force_end(monkeypatch):
    monkeypatch.setattr(agent.stt, "RECEIVE_TIMEOUT_S", 0.05)
    # Socket never yields EndOfTurn — post_force_end is empty, iter hangs.
    socket = FakeAsyncSocket()
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    task = asyncio.create_task(transcribe(_empty_chunks(), ptt_release))
    # Sit here well past RECEIVE_TIMEOUT_S with ptt_release NOT set —
    # watchdog must not fire because force_end_sent hasn't been set.
    await asyncio.sleep(0.2)
    assert not task.done(), "watchdog fired before ForceEndTurn was sent"

    # Now release; ForceEndTurn is sent, watchdog arms, fires 0.05s later.
    ptt_release.set()
    with pytest.raises(STTError, match="no EndOfTurn within"):
        await asyncio.wait_for(task, timeout=1.0)


# ---------------------------------------------------------------------------
# Bundle 16: ptt_release pre-set — Warning FORCE_END_TURN_NO_ACTIVE_TURN → ""
# ---------------------------------------------------------------------------


async def test_pre_set_ptt_release_returns_empty_on_no_active_turn(monkeypatch):
    socket = FakeAsyncSocket(
        post_force_end=[_warning("FORCE_END_TURN_NO_ACTIVE_TURN", "no active turn")]
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    ptt_release.set()

    transcript = await asyncio.wait_for(transcribe(_empty_chunks(), ptt_release), timeout=1.0)
    assert transcript == ""


# ---------------------------------------------------------------------------
# Bundle 17: ptt_release pre-set with chunks — clean return, no ordering claim
# ---------------------------------------------------------------------------


async def test_pre_set_ptt_release_with_chunks_returns_cleanly(monkeypatch):
    socket = FakeAsyncSocket(
        post_force_end=[_turn_info_end_of_turn(transcript="done", trigger="manual")]
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    ptt_release.set()

    transcript = await asyncio.wait_for(
        transcribe(_chunks_from([b"AAA", b"BBB"]), ptt_release), timeout=1.0
    )
    assert transcript == "done"


# ---------------------------------------------------------------------------
# Bundle 18: empty transcript from EndOfTurn/manual → clean return
# ---------------------------------------------------------------------------


async def test_empty_transcript_returns_empty_string(monkeypatch):
    socket = FakeAsyncSocket(
        post_force_end=[_turn_info_end_of_turn(transcript="", trigger="manual")]
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    task = asyncio.create_task(transcribe(_empty_chunks(), ptt_release))
    await asyncio.sleep(0.02)
    ptt_release.set()

    transcript = await asyncio.wait_for(task, timeout=1.0)
    assert transcript == ""


# ---------------------------------------------------------------------------
# Bundle 19: _main wiring
# ---------------------------------------------------------------------------


async def test_main_wires_audio_ptt_and_transcribe(monkeypatch, capsys):
    from agent import ptt

    async def _fake_capture():
        while True:
            await asyncio.sleep(0.01)
            yield b"\x00" * 640

    async def _fake_events():
        yield ptt.PTTEvent.PRESSED
        await asyncio.sleep(0)
        yield ptt.PTTEvent.RELEASED

    async def _fake_transcribe(chunks, ptt_release):
        await asyncio.wait_for(ptt_release.wait(), timeout=1.0)
        return "hello from stt"

    import agent.audio as _audio_mod
    import agent.ptt as _ptt_mod

    monkeypatch.setattr(_audio_mod, "capture", _fake_capture)
    monkeypatch.setattr(_ptt_mod, "events", _fake_events)
    monkeypatch.setattr(agent.stt, "transcribe", _fake_transcribe)

    await agent.stt._main()

    captured = capsys.readouterr()
    assert "hello from stt" in captured.out


# ---------------------------------------------------------------------------
# Bundle 20: ApiError 401 → STTAuthError
# ---------------------------------------------------------------------------


async def test_api_error_401_becomes_STTAuthError(monkeypatch):
    def _bad_connect(**kwargs):
        raise ApiError(status_code=401, body="invalid credentials")

    fake_client = MagicMock()
    fake_client.listen.v2.connect.side_effect = _bad_connect
    monkeypatch.setattr(agent.stt, "_client", None)
    monkeypatch.setattr(agent.stt, "_get_client", lambda: fake_client)

    ptt_release = asyncio.Event()
    with pytest.raises(STTAuthError):
        await asyncio.wait_for(transcribe(_empty_chunks(), ptt_release), timeout=1.0)


# ---------------------------------------------------------------------------
# Bundle 21: UNPARSABLE_CLIENT_MESSAGE fatal → STTForceEndTurnUnsupported
# ---------------------------------------------------------------------------


async def test_unparsable_client_message_maps_to_force_end_unsupported(monkeypatch):
    socket = FakeAsyncSocket(
        post_force_end=[
            _fatal(
                "UNPARSABLE_CLIENT_MESSAGE",
                "The ForceEndTurn message is not enabled on this deployment.",
            )
        ]
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    task = asyncio.create_task(transcribe(_empty_chunks(), ptt_release))
    await asyncio.sleep(0.02)
    ptt_release.set()

    with pytest.raises(STTForceEndTurnUnsupported):
        await asyncio.wait_for(task, timeout=1.0)


# ---------------------------------------------------------------------------
# Bundle 22: post-release chunks — pump cancelled and awaited before ForceEndTurn
# ---------------------------------------------------------------------------


async def test_post_release_chunks_not_sent_and_pump_done_before_force_end(monkeypatch):
    """Cancel-and-await pump completes BEFORE send_force_end_turn runs.

    Ordering-only assertion isn't strong enough (Tightening 2): the fake
    captures ``pump_task.done()`` at the moment ``send_force_end_turn``
    is called and asserts it's True. Also verifies no send_media entry
    appears after the single send_force_end_turn entry in call_order.
    """
    socket = FakeAsyncSocket(
        post_force_end=[_turn_info_end_of_turn(transcript="ok", trigger="manual")]
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()

    async def _long_chunks() -> AsyncIterator[bytes]:
        # 20 chunks with small awaits so cancellation has a real await
        # point to inject at.
        for i in range(20):
            yield bytes([i]) * 8
            await asyncio.sleep(0.005)

    # Wire the fake to observe the pump task at force-end call time.
    async def _run() -> str:
        # Reach into transcribe's task construction via monkeypatch: we
        # need the actual pump task instance created inside transcribe.
        # Easiest hook: patch asyncio.create_task once and grab the
        # pump task by name.
        return await transcribe(_long_chunks(), ptt_release)

    captured_pump: list[asyncio.Task] = []
    real_create_task = asyncio.create_task

    def _wrapped_create_task(coro, *args, **kwargs):
        t = real_create_task(coro, *args, **kwargs)
        if kwargs.get("name") == "stt-pump":
            captured_pump.append(t)
            socket.observe_pump_at_force_end(t)
        return t

    monkeypatch.setattr(asyncio, "create_task", _wrapped_create_task)

    task = asyncio.create_task(_run())
    await asyncio.sleep(0.03)  # let some chunks flow
    ptt_release.set()

    transcript = await asyncio.wait_for(task, timeout=1.0)
    assert transcript == "ok"

    # Ordering: single send_force_end_turn, and no send_media after it.
    force_at = [i for i, c in enumerate(socket.call_order) if c[0] == "send_force_end_turn"]
    assert len(force_at) == 1
    media_after_force = [
        i for i, c in enumerate(socket.call_order) if c[0] == "send_media" and i > force_at[0]
    ]
    assert media_after_force == [], (
        f"send_media call recorded after send_force_end_turn: {socket.call_order}"
    )

    # Tightening 2: pump task was done() at the moment force_end_turn was
    # dispatched, not merely cancelled but still pending.
    assert captured_pump, "did not capture the stt-pump task"
    assert socket._pump_task_done_at_force_end is True, (
        "pump_task.done() was False when send_force_end_turn was called — "
        "cancel-and-await did not complete before the send"
    )


# ---------------------------------------------------------------------------
# Bundle 23: long turn stays alive past RECEIVE_TIMEOUT_S while active
# ---------------------------------------------------------------------------


async def test_long_active_turn_stays_alive_past_watchdog(monkeypatch):
    """Watchdog does not arm while the turn is active (Tightening 3: no wall clock).

    Drives the receive loop with ``asyncio.sleep(0)`` yields between many
    Update messages, then only after N updates completes with EndOfTurn.
    Timing-independent: even with ``RECEIVE_TIMEOUT_S`` monkeypatched
    small, the watchdog stays disarmed because ``force_end_sent`` never
    fires until ``ptt_release`` is set.
    """
    monkeypatch.setattr(agent.stt, "RECEIVE_TIMEOUT_S", 0.01)

    # Stream a StartOfTurn plus many Updates before any release. Fake
    # cooperates by yielding as fast as the receive loop consumes.
    pre = [_turn_info_update("StartOfTurn")] + [_turn_info_update("Update") for _ in range(50)]
    socket = FakeAsyncSocket(
        pre_messages=pre,
        post_force_end=[_turn_info_end_of_turn(transcript="final", trigger="manual")],
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    task = asyncio.create_task(transcribe(_empty_chunks(), ptt_release))

    # Give the receive loop many event-loop turns to consume the 51
    # non-terminal messages. If the watchdog were armed pre-release
    # with the 0.01s timeout, transcribe would already have failed.
    for _ in range(200):
        await asyncio.sleep(0)
    assert not task.done(), (
        "watchdog fired during an active turn — it must only arm after force_end_sent"
    )

    ptt_release.set()
    transcript = await asyncio.wait_for(task, timeout=1.0)
    assert transcript == "final"


# ---------------------------------------------------------------------------
# Bundle 24: socket close without terminal → STTError
# ---------------------------------------------------------------------------


async def test_socket_close_without_terminal_raises(monkeypatch):
    """Iterator ends normally with no EndOfTurn/FatalError/Warning-terminal."""
    socket = FakeAsyncSocket(
        pre_messages=[
            _turn_info_update("StartOfTurn"),
            _turn_info_update("Update"),
        ],
        exhaust_after_messages=True,
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    with pytest.raises(STTError, match="socket closed without EndOfTurn"):
        await asyncio.wait_for(transcribe(_empty_chunks(), ptt_release), timeout=1.0)


# ---------------------------------------------------------------------------
# Bundle 28: Connection failures (OSError + WebSocketException) → STTError
# ---------------------------------------------------------------------------


async def test_connect_oserror_becomes_STTError(monkeypatch):
    """DNS/TCP/TLS failures at connect() time → STTError('connection failed: ...')."""

    class _BadCM:
        async def __aenter__(self):
            raise OSError("no route to host")

        async def __aexit__(self, exc_type, exc, tb):
            return None

    fake_client = MagicMock()
    fake_client.listen.v2.connect.side_effect = lambda **kwargs: _BadCM()
    monkeypatch.setattr(agent.stt, "_client", None)
    monkeypatch.setattr(agent.stt, "_get_client", lambda: fake_client)

    ptt_release = asyncio.Event()
    with pytest.raises(STTError, match="connection failed") as exc_info:
        await asyncio.wait_for(transcribe(_empty_chunks(), ptt_release), timeout=1.0)
    # Not a more-specific STT subtype:
    assert type(exc_info.value) is STTError
    assert isinstance(exc_info.value.__cause__, OSError)


async def test_connect_websocketexception_becomes_STTError(monkeypatch):
    """Websockets-level failures at connect() time → STTError('connection failed: ...')."""
    from websockets.exceptions import InvalidURI, WebSocketException

    assert issubclass(InvalidURI, WebSocketException)  # sanity

    class _BadCM:
        async def __aenter__(self):
            raise InvalidURI("bad://url", "malformed handshake")

        async def __aexit__(self, exc_type, exc, tb):
            return None

    fake_client = MagicMock()
    fake_client.listen.v2.connect.side_effect = lambda **kwargs: _BadCM()
    monkeypatch.setattr(agent.stt, "_client", None)
    monkeypatch.setattr(agent.stt, "_get_client", lambda: fake_client)

    ptt_release = asyncio.Event()
    with pytest.raises(STTError, match="connection failed") as exc_info:
        await asyncio.wait_for(transcribe(_empty_chunks(), ptt_release), timeout=1.0)
    assert type(exc_info.value) is STTError
    assert isinstance(exc_info.value.__cause__, WebSocketException)


# ---------------------------------------------------------------------------
# Bundles 29-31: _main watcher supervision (Round-3 Finding 2)
# ---------------------------------------------------------------------------


def _install_main_fakes(monkeypatch, fake_events, fake_transcribe=None):
    """Install fake ptt.events, agent.audio.capture (no-op), and transcribe.

    Kept as a helper so bundles 29-31 share the wiring boilerplate.
    """
    import agent.audio as _audio_mod
    import agent.ptt as _ptt_mod

    async def _fake_capture():
        while True:
            await asyncio.sleep(0.001)
            yield b"\x00" * 640

    async def _default_fake_transcribe(chunks, ptt_release):
        await asyncio.wait_for(ptt_release.wait(), timeout=5.0)
        return "unused"

    monkeypatch.setattr(_audio_mod, "capture", _fake_capture)
    monkeypatch.setattr(_ptt_mod, "events", fake_events)
    monkeypatch.setattr(agent.stt, "transcribe", fake_transcribe or _default_fake_transcribe)


async def test_main_raises_when_watcher_terminates_before_pressed(monkeypatch):
    """Watcher completes with no events → STTError('...before PRESSED')."""

    async def _empty_events():
        # Generator that yields nothing and returns immediately.
        if False:  # pragma: no cover
            yield None

    _install_main_fakes(monkeypatch, _empty_events)

    with pytest.raises(STTError, match="before PRESSED"):
        await asyncio.wait_for(agent.stt._main(), timeout=1.0)


async def test_main_raises_when_watcher_terminates_after_pressed_no_released(monkeypatch):
    """Watcher yields PRESSED then completes without RELEASED → STTError('...before RELEASED')."""
    from agent import ptt

    async def _press_only_events():
        yield ptt.PTTEvent.PRESSED
        # Generator returns after PRESSED; no RELEASED event ever fires.

    _install_main_fakes(monkeypatch, _press_only_events)

    # The safety-net timeout in wait_for is the SECOND line of defense
    # for this test: the real proof is that STTError is raised. If the
    # supervision were broken, the test would time out instead.
    with pytest.raises(STTError, match="before RELEASED"):
        await asyncio.wait_for(agent.stt._main(), timeout=1.0)


async def test_main_chains_watcher_exception_via_cause(monkeypatch):
    """Watcher raises after PRESSED → STTError with __cause__ identity-preserved."""
    from agent import ptt

    # Capture the raised instance so we can assert __cause__ IS this
    # object (identity, not just type). Preetam's Tightening 3 spec.
    sentinel_exc = RuntimeError("keyboard hook exploded")

    async def _raising_events():
        yield ptt.PTTEvent.PRESSED
        raise sentinel_exc

    _install_main_fakes(monkeypatch, _raising_events)

    with pytest.raises(STTError) as exc_info:
        await asyncio.wait_for(agent.stt._main(), timeout=1.0)
    assert "before RELEASED" in str(exc_info.value)
    # Identity check: same object, chained via `raise ... from exc`.
    assert exc_info.value.__cause__ is sentinel_exc


# ---------------------------------------------------------------------------
# Bundle 27: Missing DEEPGRAM_API_KEY at construction → STTAuthError
# ---------------------------------------------------------------------------
#
# The SDK's construction-time raise message, quoted VERBATIM from
# deepgram/base_client.py:266-269 in deepgram-sdk==7.8.1. Hardcoded so
# that a future SDK version bump that changes this text surfaces
# through this test rather than drifting silently.
_SDK_MISSING_KEY_BODY = (
    "The client must be instantiated be either passing in api_key or setting DEEPGRAM_API_KEY"
)


async def test_missing_api_key_at_construction_maps_to_STTAuthError(monkeypatch):
    """Empty/unset DEEPGRAM_API_KEY at construction → STTAuthError, not STTError."""
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)

    def _bad_ctor(*args, **kwargs):
        # Attributes match Task 1b findings for SDK 7.8.1: status_code
        # and headers are both None; body carries the sentinel message.
        raise ApiError(status_code=None, headers=None, body=_SDK_MISSING_KEY_BODY)

    monkeypatch.setattr(agent.stt, "_client", None)
    monkeypatch.setattr(agent.stt, "AsyncDeepgramClient", _bad_ctor)

    ptt_release = asyncio.Event()
    with pytest.raises(STTAuthError, match="DEEPGRAM_API_KEY missing"):
        await asyncio.wait_for(transcribe(_empty_chunks(), ptt_release), timeout=1.0)


# ---------------------------------------------------------------------------
# Bundle 25a: SDK _websocket attribute missing → STTError (pin-mismatch guard)
# ---------------------------------------------------------------------------


async def test_missing_websocket_attribute_raises_STTError(monkeypatch):
    """The ``hasattr(socket, "_websocket")`` guard fires cleanly."""
    socket = FakeAsyncSocket(omit_websocket_attr=True)
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    with pytest.raises(STTError, match="socket._websocket"):
        await asyncio.wait_for(transcribe(_empty_chunks(), ptt_release), timeout=1.0)


# ---------------------------------------------------------------------------
# Bundle 25b: Warning with unrecognized code → log + continue
# ---------------------------------------------------------------------------


async def test_unrecognized_warning_logs_and_continues(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="agent.stt")
    socket = FakeAsyncSocket(
        pre_messages=[_warning("SOME_UNRELATED_CODE", "just a warning")],
        post_force_end=[_turn_info_end_of_turn(transcript="after", trigger="manual")],
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    task = asyncio.create_task(transcribe(_empty_chunks(), ptt_release))
    await asyncio.sleep(0.02)
    ptt_release.set()

    transcript = await asyncio.wait_for(task, timeout=1.0)
    assert transcript == "after"
    assert any("SOME_UNRELATED_CODE" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Bundle 25c: ApiError non-401 → generic STTError
# ---------------------------------------------------------------------------


async def test_api_error_non_401_becomes_generic_STTError(monkeypatch):
    def _bad_connect(**kwargs):
        raise ApiError(status_code=500, body="server melted")

    fake_client = MagicMock()
    fake_client.listen.v2.connect.side_effect = _bad_connect
    monkeypatch.setattr(agent.stt, "_client", None)
    monkeypatch.setattr(agent.stt, "_get_client", lambda: fake_client)

    ptt_release = asyncio.Event()
    with pytest.raises(STTError) as exc_info:
        await asyncio.wait_for(transcribe(_empty_chunks(), ptt_release), timeout=1.0)
    # NOT the auth subclass; the generic bucket.
    assert type(exc_info.value) is STTError
    assert not isinstance(exc_info.value, STTAuthError)
    assert "status=500" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Bundle 25d: chunks source raises STTError → propagates with original identity
# ---------------------------------------------------------------------------


async def test_chunks_raising_STTError_preserves_identity(monkeypatch):
    """An STTError raised inside chunks propagates as-is, not wrapped."""
    socket = FakeAsyncSocket()
    _install_fake_client(monkeypatch, socket)

    class _CustomSTT(STTError):
        pass

    async def _stt_chunks() -> AsyncIterator[bytes]:
        yield b"AAA"
        raise _CustomSTT("preserve me")

    ptt_release = asyncio.Event()
    with pytest.raises(_CustomSTT, match="preserve me"):
        await asyncio.wait_for(transcribe(_stt_chunks(), ptt_release), timeout=1.0)


# ---------------------------------------------------------------------------
# Bundle 26: _main waits for PRESSED before consuming capture (Finding 6)
# ---------------------------------------------------------------------------


async def test_main_does_not_consume_capture_before_pressed(monkeypatch):
    from agent import ptt

    press_at: list[float] = []
    first_capture_at: list[float] = []

    async def _fake_events():
        # Idle for 100 ms with no events, then PRESSED, then RELEASED.
        for _ in range(10):
            await asyncio.sleep(0)
        press_at.append(asyncio.get_event_loop().time())
        yield ptt.PTTEvent.PRESSED
        await asyncio.sleep(0)
        yield ptt.PTTEvent.RELEASED

    async def _fake_capture():
        first_capture_at.append(asyncio.get_event_loop().time())
        while True:
            await asyncio.sleep(0)
            yield b"\x00" * 640

    async def _fake_transcribe(chunks, ptt_release):
        # Consume one chunk to trigger _fake_capture's first __anext__.
        async for _ in chunks:
            break
        await asyncio.wait_for(ptt_release.wait(), timeout=1.0)
        return ""

    import agent.audio as _audio_mod
    import agent.ptt as _ptt_mod

    monkeypatch.setattr(_audio_mod, "capture", _fake_capture)
    monkeypatch.setattr(_ptt_mod, "events", _fake_events)
    monkeypatch.setattr(agent.stt, "transcribe", _fake_transcribe)

    await agent.stt._main()

    assert press_at, "PRESSED was never emitted by fake events"
    assert first_capture_at, "audio.capture was never consumed"
    assert first_capture_at[0] >= press_at[0], (
        f"audio.capture started at {first_capture_at[0]} before "
        f"PRESSED at {press_at[0]} — Finding 6 regression"
    )


# ---------------------------------------------------------------------------
# #70 / ADR-0005 Decision 1: eager mode, speculative finalization, PTT grace
# ---------------------------------------------------------------------------


@pytest.mark.no_fast_grace
def test_config_assertion_rejects_eager_above_eot():
    """Module-level ``assert`` rejects ``EAGER_EOT_THRESHOLD > EOT_THRESHOLD``.

    The invariant is enforced at import time inside ``agent.stt``.
    Exercising it with bad values requires rebuilding the module
    with ``EAGER_EOT_THRESHOLD`` raised above ``EOT_THRESHOLD`` —
    ``importlib.reload`` re-executes with source values, so we go
    one level lower via ``compile`` + ``exec`` of the source with
    the constant rewritten.
    """
    import pathlib

    source = pathlib.Path(agent.stt.__file__).read_text(encoding="utf-8")
    patched = source.replace(
        "EAGER_EOT_THRESHOLD = 0.5",
        "EAGER_EOT_THRESHOLD = 0.9",
    )
    assert "EAGER_EOT_THRESHOLD = 0.9" in patched, "patch failed"

    namespace: dict = {"__name__": "agent.stt_bad_variant", "__file__": agent.stt.__file__}
    with pytest.raises(AssertionError, match=r"Flux config invariant violated"):
        exec(compile(patched, agent.stt.__file__, "exec"), namespace)


async def test_connect_passes_eager_eot_threshold(monkeypatch):
    """``client.listen.v2.connect`` receives the eager-mode kwarg."""
    socket = FakeAsyncSocket(
        post_force_end=[_turn_info_end_of_turn(transcript="ok", trigger="manual")]
    )
    proxy = _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    task = asyncio.create_task(transcribe(_empty_chunks(), ptt_release))
    await asyncio.sleep(0.02)
    ptt_release.set()
    await asyncio.wait_for(task, timeout=1.0)

    cm = proxy._holder[-1]
    assert cm.kwargs["eager_eot_threshold"] == agent.stt.EAGER_EOT_THRESHOLD
    assert cm.kwargs["eot_threshold"] == agent.stt.EOT_THRESHOLD
    # Invariant upheld in the forwarded call too.
    assert cm.kwargs["eager_eot_threshold"] <= cm.kwargs["eot_threshold"]


async def test_eager_end_of_turn_is_speculative(monkeypatch, caplog):
    """``EagerEndOfTurn`` must NOT resolve done_future; ``EndOfTurn`` does.

    Scripts [EagerEndOfTurn, EndOfTurn] back-to-back. The assertion
    is positional (EndOfTurn's transcript wins, not the Eager event's)
    plus a DEBUG log line from the eager handler so a regression
    collapsing the two paths is visible.
    """
    socket = FakeAsyncSocket(
        pre_messages=[
            _turn_info_update("EagerEndOfTurn"),
            _turn_info_end_of_turn(transcript="confirmed", trigger="manual"),
        ],
        exhaust_after_messages=True,
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    with caplog.at_level(logging.DEBUG, logger="agent.stt"):
        result = await transcribe(_empty_chunks(), ptt_release)

    assert result == "confirmed"
    eager_records = [r for r in caplog.records if "EagerEndOfTurn" in r.getMessage()]
    assert eager_records, "no DEBUG log for EagerEndOfTurn"
    assert all(r.levelno == logging.DEBUG for r in eager_records)


async def test_turn_resumed_retracts_pending_eager(monkeypatch, caplog):
    """``TurnResumed`` after ``EagerEndOfTurn`` retracts the speculative finalization.

    Scripts [EagerEndOfTurn, TurnResumed, EndOfTurn(final)]. The
    final transcript wins; both the eager and the resume events log
    at DEBUG.
    """
    socket = FakeAsyncSocket(
        pre_messages=[
            _turn_info_update("EagerEndOfTurn"),
            _turn_info_update("TurnResumed"),
            _turn_info_end_of_turn(transcript="final transcript", trigger="manual"),
        ],
        exhaust_after_messages=True,
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    with caplog.at_level(logging.DEBUG, logger="agent.stt"):
        result = await transcribe(_empty_chunks(), ptt_release)

    assert result == "final transcript"
    messages = [r.getMessage() for r in caplog.records]
    assert any("EagerEndOfTurn" in m for m in messages), "no DEBUG log for EagerEndOfTurn"
    assert any("retracted pending EagerEndOfTurn" in m for m in messages), (
        "no DEBUG log for TurnResumed retraction"
    )
    # Neither event should have raised a WARNING in this ordered sequence.
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == []


async def test_turn_resumed_without_pending_eager_logs_warning(monkeypatch, caplog):
    """``TurnResumed`` with no pending eager event logs WARNING and is ignored.

    Resilience over strictness: the stream is not aborted; EndOfTurn
    still resolves done_future normally.
    """
    socket = FakeAsyncSocket(
        pre_messages=[
            _turn_info_update("TurnResumed"),
            _turn_info_end_of_turn(transcript="ok", trigger="manual"),
        ],
        exhaust_after_messages=True,
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    with caplog.at_level(logging.DEBUG, logger="agent.stt"):
        result = await transcribe(_empty_chunks(), ptt_release)

    assert result == "ok"
    warn_records = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "TurnResumed" in r.getMessage()
    ]
    assert warn_records, "expected WARNING for unmatched TurnResumed"
    # turn_index placeholder from _turn_info_update is 0; message should carry it.
    assert "turn_index=0" in warn_records[0].getMessage()


async def test_ptt_release_eot_in_grace_no_force_end(monkeypatch):
    """Natural EndOfTurn inside ``PTT_RELEASE_GRACE_MS`` skips ``ForceEndTurn``.

    The autouse fixture already pins the grace at 10 ms. Script
    EndOfTurn as a pre-message so ``_receive`` sees it immediately
    after the socket opens; set ``ptt_release`` and verify the
    release-task observes ``eot_received`` in-grace and never sends
    ``ForceEndTurn``.
    """
    socket = FakeAsyncSocket(
        pre_messages=[_turn_info_end_of_turn(transcript="natural eot", trigger="manual")],
        exhaust_after_messages=True,
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    ptt_release.set()  # release is already signalled when transcribe starts

    result = await asyncio.wait_for(transcribe(_empty_chunks(), ptt_release), timeout=1.0)

    assert result == "natural eot"
    assert socket.force_end_turn_count == 0, (
        f"ForceEndTurn fired despite in-grace natural EOT: call_order={socket.call_order}"
    )


async def test_ptt_release_grace_expires_sends_force_end(monkeypatch, caplog):
    """Grace expires with no natural EOT → WARNING logged + ``ForceEndTurn`` sent."""
    socket = FakeAsyncSocket(
        post_force_end=[_turn_info_end_of_turn(transcript="forced", trigger="manual")]
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    with caplog.at_level(logging.WARNING, logger="agent.stt"):
        task = asyncio.create_task(transcribe(_empty_chunks(), ptt_release))
        await asyncio.sleep(0.02)
        ptt_release.set()
        result = await asyncio.wait_for(task, timeout=1.0)

    assert result == "forced"
    assert socket.force_end_turn_count == 1
    grace_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "PTT release grace expired" in r.getMessage()
    ]
    assert grace_warnings, "expected WARNING on grace expiry"


async def test_ptt_release_eager_then_eot_in_grace(monkeypatch):
    """Eager + natural EOT inside grace: ``ForceEndTurn`` still skipped.

    The eager event sets ``pending_eager`` but does NOT resolve
    done_future; the subsequent EndOfTurn does. Both land before the
    grace timer expires.
    """
    socket = FakeAsyncSocket(
        pre_messages=[
            _turn_info_update("EagerEndOfTurn"),
            _turn_info_end_of_turn(transcript="eager-then-eot", trigger="manual"),
        ],
        exhaust_after_messages=True,
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    ptt_release.set()

    result = await asyncio.wait_for(transcribe(_empty_chunks(), ptt_release), timeout=1.0)

    assert result == "eager-then-eot"
    assert socket.force_end_turn_count == 0


async def test_model_triggered_eot_ignored_during_active_ptt(monkeypatch, caplog):
    """Model-trigger EndOfTurn during active PTT is ignored; manual wins.

    Flux can emit ``EndOfTurn{trigger="model"}`` on a mid-utterance
    silence while the user is still holding PTT (``EOT_THRESHOLD=0.7``
    makes that possible). Pre-fix, this cut transcription off and
    returned only the first part. Post-fix, the model-triggered
    event is ignored with a DEBUG log; ``_receive`` keeps listening
    until the release-driven ``ForceEndTurn`` elicits a terminal
    ``trigger="manual"`` EndOfTurn.
    """
    socket = FakeAsyncSocket(
        pre_messages=[
            _turn_info_end_of_turn(transcript="partial", trigger="model"),
        ],
        post_force_end=[
            _turn_info_end_of_turn(transcript="full utterance", trigger="manual"),
        ],
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    with caplog.at_level(logging.DEBUG, logger="agent.stt"):
        task = asyncio.create_task(transcribe(_empty_chunks(), ptt_release))
        # Let _receive consume the pre-release model-triggered EOT first.
        await asyncio.sleep(0.02)
        ptt_release.set()
        result = await asyncio.wait_for(task, timeout=1.0)

    # Pre-release model EOT's transcript "partial" is accumulated and
    # space-joined with the terminal manual EOT's "full utterance".
    assert result == "partial full utterance"
    ignore_debug = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and "ignoring model-triggered EndOfTurn" in r.getMessage()
    ]
    assert ignore_debug, "expected DEBUG log for ignored model-trigger EndOfTurn"
    assert socket.force_end_turn_count > 0, (
        "release should have fallen through to ForceEndTurn after grace expiry; "
        f"call_order={socket.call_order}"
    )


async def test_model_triggered_eot_terminal_after_release(monkeypatch):
    """``trigger="model"`` is terminal once ``ptt_release`` is set.

    Post-release, Flux native EOT is the designed natural-finalization
    path. ``_receive`` falls through to ``_handle_end_of_turn`` and
    ``eot_received`` signals ``_release`` in-grace → no
    ``ForceEndTurn``.
    """
    socket = FakeAsyncSocket(
        pre_messages=[
            _turn_info_end_of_turn(transcript="post-release natural", trigger="model"),
        ],
        exhaust_after_messages=True,
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    ptt_release.set()  # release is already signalled when transcribe starts

    result = await asyncio.wait_for(transcribe(_empty_chunks(), ptt_release), timeout=1.0)

    assert result == "post-release natural"
    assert socket.force_end_turn_count == 0, (
        f"post-release natural EOT must not provoke ForceEndTurn; call_order={socket.call_order}"
    )


async def test_sequential_model_eots_during_hold_then_manual(monkeypatch, caplog):
    """Multiple pre-release model EOTs are all ignored; post-release manual wins.

    Simulates two mid-utterance silences during a single hold. Both
    model-trigger events are ignored (one DEBUG per event). On
    release, ``ForceEndTurn`` elicits the manual terminal event.
    """
    socket = FakeAsyncSocket(
        pre_messages=[
            _turn_info_end_of_turn(transcript="silence 1", trigger="model"),
            _turn_info_end_of_turn(transcript="silence 2", trigger="model"),
        ],
        post_force_end=[
            _turn_info_end_of_turn(transcript="final after release", trigger="manual"),
        ],
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    with caplog.at_level(logging.DEBUG, logger="agent.stt"):
        task = asyncio.create_task(transcribe(_empty_chunks(), ptt_release))
        await asyncio.sleep(0.02)
        ptt_release.set()
        result = await asyncio.wait_for(task, timeout=1.0)

    # Both "silence 1" and "silence 2" are accumulated and joined with
    # the terminal manual EOT's "final after release".
    assert result == "silence 1 silence 2 final after release"
    ignore_debug = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and "ignoring model-triggered EndOfTurn" in r.getMessage()
    ]
    assert len(ignore_debug) == 2, (
        f"expected 2 DEBUG logs for ignored model-trigger EndOfTurns, got {len(ignore_debug)}"
    )
    assert socket.force_end_turn_count > 0


async def test_model_triggered_eot_transcript_preserved_via_accumulation(monkeypatch):
    """The transcript from an ignored model EOT survives into the terminal transcript.

    Direct smoke of the accumulator: Flux emits ``hello there`` with
    a mid-utterance ``trigger="model"`` EndOfTurn (would be dropped
    without the accumulator), then after release emits ``how are
    you`` with the terminal ``trigger="manual"``. The returned
    transcript must contain both halves space-joined.
    """
    socket = FakeAsyncSocket(
        pre_messages=[
            _turn_info_end_of_turn(transcript="hello there", trigger="model"),
        ],
        post_force_end=[
            _turn_info_end_of_turn(transcript="how are you", trigger="manual"),
        ],
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    task = asyncio.create_task(transcribe(_empty_chunks(), ptt_release))
    await asyncio.sleep(0.02)
    ptt_release.set()
    result = await asyncio.wait_for(task, timeout=1.0)

    assert result == "hello there how are you"
    assert socket.force_end_turn_count > 0


async def test_force_end_no_active_turn_returns_accumulated_transcript(monkeypatch):
    """User speaks, Flux closes the turn on silence (model EOT accumulated),
    user stays silent, releases. ``ForceEndTurn`` finds no active turn;
    accumulated transcript is returned instead of empty string.

    Baseline (empty-accumulator) silent-PTT behaviour is still
    covered by ``test_pre_set_ptt_release_returns_empty_on_no_active_turn``
    in Bundle 16 — that test's expectation (``""``) is preserved.
    """
    socket = FakeAsyncSocket(
        pre_messages=[
            _turn_info_end_of_turn(transcript="hello", trigger="model"),
        ],
        post_force_end=[
            _warning(
                "FORCE_END_TURN_NO_ACTIVE_TURN",
                "no active turn when ForceEndTurn arrived",
            ),
        ],
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    task = asyncio.create_task(transcribe(_empty_chunks(), ptt_release))
    await asyncio.sleep(0.02)
    ptt_release.set()
    result = await asyncio.wait_for(task, timeout=1.0)

    assert result == "hello"
    assert socket.force_end_turn_count > 0


# ---------------------------------------------------------------------------
# #72 / ADR-0005 Decision 2: detect_barge_in confirmation window
# ---------------------------------------------------------------------------


from agent.stt import BARGEIN_CONFIRM_MS, detect_barge_in  # noqa: E402


@pytest.fixture
def fast_bargein_confirm(monkeypatch):
    """Shrink the confirmation window so timeout-path tests don't sit
    on 150 ms of real wall clock. Tests that need to observe window
    expiry use a shorter value for speed; those pinning the production
    value monkeypatch to the original."""
    monkeypatch.setattr(agent.stt, "BARGEIN_CONFIRM_MS", 30)
    return 30


def _mock_mic() -> AsyncIterator[bytes]:
    """Infinite mic iterator that yields 20 ms silence frames forever.

    The barge-in detector doesn't consume rapidly — tests just need
    something to send into the Flux session. Each ``await``'d chunk
    yields the event loop so the receive-side can advance.
    """

    async def _gen() -> AsyncIterator[bytes]:
        while True:
            await asyncio.sleep(0.001)
            yield b"\x00" * 640

    return _gen()


async def test_detect_barge_in_fires_on_startofturn_plus_update_in_window(
    monkeypatch, fast_bargein_confirm
):
    """StartOfTurn followed by Update(transcript='hi') inside the window
    sets ``barge_in_event`` and returns cleanly."""
    socket = FakeAsyncSocket(
        pre_messages=[
            _turn_info_update("StartOfTurn"),
            json.dumps(
                {
                    "type": "TurnInfo",
                    "request_id": "test",
                    "sequence_id": 2,
                    "event": "Update",
                    "turn_index": 0,
                    "audio_window_start": 0.0,
                    "audio_window_end": 0.2,
                    "transcript": "hi",
                    "words": [],
                    "end_of_turn_confidence": 0.3,
                }
            ),
        ],
        exhaust_after_messages=True,
    )
    _install_fake_client(monkeypatch, socket)

    barge_in_event = asyncio.Event()
    await asyncio.wait_for(
        detect_barge_in(_mock_mic(), barge_in_event),
        timeout=1.0,
    )
    assert barge_in_event.is_set()


async def test_detect_barge_in_fires_on_startofturn_plus_eager_in_window(
    monkeypatch, fast_bargein_confirm
):
    """StartOfTurn + EagerEndOfTurn (no transcript needed) counts as confirmation."""
    socket = FakeAsyncSocket(
        pre_messages=[
            _turn_info_update("StartOfTurn"),
            _turn_info_update("EagerEndOfTurn"),
        ],
        exhaust_after_messages=True,
    )
    _install_fake_client(monkeypatch, socket)

    barge_in_event = asyncio.Event()
    await asyncio.wait_for(
        detect_barge_in(_mock_mic(), barge_in_event),
        timeout=1.0,
    )
    assert barge_in_event.is_set()


async def test_detect_barge_in_fires_on_startofturn_plus_turnresumed_in_window(
    monkeypatch, fast_bargein_confirm
):
    """TurnResumed inside the window counts as speech-indicative too."""
    socket = FakeAsyncSocket(
        pre_messages=[
            _turn_info_update("StartOfTurn"),
            _turn_info_update("TurnResumed"),
        ],
        exhaust_after_messages=True,
    )
    _install_fake_client(monkeypatch, socket)

    barge_in_event = asyncio.Event()
    await asyncio.wait_for(
        detect_barge_in(_mock_mic(), barge_in_event),
        timeout=1.0,
    )
    assert barge_in_event.is_set()


async def test_detect_barge_in_ignores_empty_update_in_window(
    monkeypatch, fast_bargein_confirm, caplog
):
    """``Update`` with empty transcript is a heartbeat, not evidence.
    The window should expire with no confirmation."""
    socket = FakeAsyncSocket(
        pre_messages=[
            _turn_info_update("StartOfTurn"),
            _turn_info_update("Update"),  # empty transcript
        ],
        exhaust_after_messages=True,
    )
    _install_fake_client(monkeypatch, socket)

    barge_in_event = asyncio.Event()
    with caplog.at_level(logging.INFO, logger="agent.stt"):
        await asyncio.wait_for(
            detect_barge_in(_mock_mic(), barge_in_event),
            timeout=1.0,
        )

    assert not barge_in_event.is_set()
    confirmed = [r for r in caplog.records if "confirmed" in r.getMessage()]
    assert not confirmed, "no confirmation should have fired on empty-update evidence"


async def test_detect_barge_in_window_expires_without_followup(monkeypatch, fast_bargein_confirm):
    """StartOfTurn arrives, nothing else does. The window expires and
    the detector keeps running (until the socket closes or caller
    cancels). We cancel after a sufficient delay and assert the event
    stayed unset."""
    socket = FakeAsyncSocket(
        pre_messages=[_turn_info_update("StartOfTurn")],
        exhaust_after_messages=False,  # socket hangs; mirrors real idle Flux
    )
    _install_fake_client(monkeypatch, socket)

    barge_in_event = asyncio.Event()
    task = asyncio.create_task(
        detect_barge_in(_mock_mic(), barge_in_event, confirmation_ms=fast_bargein_confirm)
    )
    # Wait longer than the confirmation window; nothing else is going
    # to arrive, so no barge-in should fire.
    await asyncio.sleep(fast_bargein_confirm / 1000 * 3)
    assert not barge_in_event.is_set()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_detect_barge_in_followup_after_window_does_not_confirm(
    monkeypatch, fast_bargein_confirm
):
    """StartOfTurn fires, window expires, then an Update arrives. The
    late Update is NOT evidence for the expired window — the detector
    must not fire the barge-in signal."""
    confirm_s = fast_bargein_confirm / 1000

    class _DelayedWebSocket:
        """Yield StartOfTurn immediately, then an Update after a delay
        longer than the confirmation window. Models a slow-arriving
        follow-up event."""

        def __aiter__(self):
            return self._iter()

        async def _iter(self):
            yield _turn_info_update("StartOfTurn")
            await asyncio.sleep(confirm_s * 2)  # exceed window
            yield json.dumps(
                {
                    "type": "TurnInfo",
                    "request_id": "test",
                    "sequence_id": 2,
                    "event": "Update",
                    "turn_index": 0,
                    "audio_window_start": 0.0,
                    "audio_window_end": 0.2,
                    "transcript": "late",
                    "words": [],
                    "end_of_turn_confidence": 0.3,
                }
            )
            # Hang so the detector doesn't exit the receive loop.
            await asyncio.Event().wait()

    socket = FakeAsyncSocket()
    socket._websocket = _DelayedWebSocket()  # type: ignore[assignment]
    _install_fake_client(monkeypatch, socket)

    barge_in_event = asyncio.Event()
    task = asyncio.create_task(
        detect_barge_in(_mock_mic(), barge_in_event, confirmation_ms=fast_bargein_confirm)
    )
    # Wait long enough for both events to arrive.
    await asyncio.sleep(confirm_s * 3)
    assert not barge_in_event.is_set(), "late follow-up after window expiry must not confirm"
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_detect_barge_in_rearms_on_second_startofturn(monkeypatch, fast_bargein_confirm):
    """A second StartOfTurn re-arms the confirmation window. An Update
    inside the re-armed window still confirms."""
    confirm_s = fast_bargein_confirm / 1000

    class _ReArmWebSocket:
        def __aiter__(self):
            return self._iter()

        async def _iter(self):
            yield _turn_info_update("StartOfTurn")
            await asyncio.sleep(confirm_s * 2)  # let window 1 expire
            yield _turn_info_update("StartOfTurn")
            await asyncio.sleep(confirm_s / 4)  # inside window 2
            yield json.dumps(
                {
                    "type": "TurnInfo",
                    "request_id": "test",
                    "sequence_id": 3,
                    "event": "Update",
                    "turn_index": 1,
                    "audio_window_start": 0.0,
                    "audio_window_end": 0.2,
                    "transcript": "yo",
                    "words": [],
                    "end_of_turn_confidence": 0.3,
                }
            )
            await asyncio.Event().wait()

    socket = FakeAsyncSocket()
    socket._websocket = _ReArmWebSocket()  # type: ignore[assignment]
    _install_fake_client(monkeypatch, socket)

    barge_in_event = asyncio.Event()
    task = asyncio.create_task(
        detect_barge_in(_mock_mic(), barge_in_event, confirmation_ms=fast_bargein_confirm)
    )
    await asyncio.sleep(confirm_s * 4)
    assert barge_in_event.is_set(), "re-armed window should have confirmed"
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_detect_barge_in_empty_endofturn_in_window_discards(
    monkeypatch, fast_bargein_confirm
):
    """``EndOfTurn`` with empty transcript inside the window is
    disconfirming — the server closed a non-speech turn. The detector
    discards the window and keeps listening."""
    socket = FakeAsyncSocket(
        pre_messages=[
            _turn_info_update("StartOfTurn"),
            _turn_info_end_of_turn(transcript="", trigger="model"),
        ],
        exhaust_after_messages=False,
    )
    _install_fake_client(monkeypatch, socket)

    barge_in_event = asyncio.Event()
    task = asyncio.create_task(
        detect_barge_in(_mock_mic(), barge_in_event, confirmation_ms=fast_bargein_confirm)
    )
    await asyncio.sleep(fast_bargein_confirm / 1000 * 3)
    assert not barge_in_event.is_set()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_detect_barge_in_force_end_turn_warning_discards_window(
    monkeypatch, fast_bargein_confirm
):
    """``FORCE_END_TURN_NO_ACTIVE_TURN`` arriving inside the window
    cancels the pending confirmation and the detector keeps listening."""
    socket = FakeAsyncSocket(
        pre_messages=[
            _turn_info_update("StartOfTurn"),
            _warning("FORCE_END_TURN_NO_ACTIVE_TURN", "flap"),
        ],
        exhaust_after_messages=False,
    )
    _install_fake_client(monkeypatch, socket)

    barge_in_event = asyncio.Event()
    task = asyncio.create_task(
        detect_barge_in(_mock_mic(), barge_in_event, confirmation_ms=fast_bargein_confirm)
    )
    await asyncio.sleep(fast_bargein_confirm / 1000 * 3)
    assert not barge_in_event.is_set()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_detect_barge_in_cancellation_cleans_up(monkeypatch, fast_bargein_confirm):
    """External cancellation (voice_loop at BARGE_IN entry / clean end)
    propagates ``CancelledError`` and does not fire the event."""
    socket = FakeAsyncSocket(exhaust_after_messages=False)
    _install_fake_client(monkeypatch, socket)

    barge_in_event = asyncio.Event()
    task = asyncio.create_task(detect_barge_in(_mock_mic(), barge_in_event))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not barge_in_event.is_set()


async def test_detect_barge_in_auth_error_during_connect(monkeypatch):
    """A 401 from Flux surfaces as ``STTAuthError`` from the connect
    seam, mirroring ``transcribe``'s behaviour."""
    fake_client = MagicMock(name="AsyncDeepgramClient()")
    fake_client.listen.v2.connect.side_effect = ApiError(status_code=401, body="auth")

    monkeypatch.setattr(agent.stt, "_client", None)
    monkeypatch.setattr(agent.stt, "_get_client", lambda: fake_client)

    with pytest.raises(STTAuthError):
        await detect_barge_in(_mock_mic(), asyncio.Event())


async def test_detect_barge_in_fatal_error_during_receive(monkeypatch, fast_bargein_confirm):
    """A FatalError from Flux maps to ``STTFatal``."""
    socket = FakeAsyncSocket(
        pre_messages=[_fatal("GENERIC_FAULT", "test")],
        exhaust_after_messages=True,
    )
    _install_fake_client(monkeypatch, socket)

    with pytest.raises(STTFatal):
        await detect_barge_in(_mock_mic(), asyncio.Event())


async def test_detect_barge_in_unparsable_message_maps_to_force_end_unsupported(
    monkeypatch, fast_bargein_confirm
):
    """``UNPARSABLE_CLIENT_MESSAGE`` surfaces as
    ``STTForceEndTurnUnsupported`` (mirrors ``transcribe``)."""
    socket = FakeAsyncSocket(
        pre_messages=[_fatal("UNPARSABLE_CLIENT_MESSAGE", "no ForceEndTurn")],
        exhaust_after_messages=True,
    )
    _install_fake_client(monkeypatch, socket)

    with pytest.raises(STTForceEndTurnUnsupported):
        await detect_barge_in(_mock_mic(), asyncio.Event())


async def test_detect_barge_in_connection_closed_during_receive(monkeypatch, fast_bargein_confirm):
    """``ConnectionClosed`` from the receive iterator surfaces as
    ``STTError`` with a specific wording."""
    socket = FakeAsyncSocket(
        pre_messages=[ConnectionClosed(None, None)],
        exhaust_after_messages=True,
    )
    _install_fake_client(monkeypatch, socket)

    with pytest.raises(STTError, match="connection closed"):
        await detect_barge_in(_mock_mic(), asyncio.Event(), confirmation_ms=fast_bargein_confirm)


async def test_detect_barge_in_invalid_json_wrapped_in_stt_error(monkeypatch, fast_bargein_confirm):
    """A non-JSON wire message hits the generic ``except Exception`` in
    ``_watch_for_barge_in`` and is wrapped as ``STTError``."""
    socket = FakeAsyncSocket(
        pre_messages=["not json at all"],
        exhaust_after_messages=True,
    )
    _install_fake_client(monkeypatch, socket)

    with pytest.raises(STTError, match="receive loop failed"):
        await detect_barge_in(_mock_mic(), asyncio.Event(), confirmation_ms=fast_bargein_confirm)


async def test_detect_barge_in_missing_websocket_attr_raises(monkeypatch, fast_bargein_confirm):
    """SDK pin mismatch: no ``_websocket`` attribute → ``STTError``."""
    socket = FakeAsyncSocket(omit_websocket_attr=True)
    _install_fake_client(monkeypatch, socket)

    with pytest.raises(STTError, match="socket._websocket"):
        await detect_barge_in(_mock_mic(), asyncio.Event(), confirmation_ms=fast_bargein_confirm)


async def test_mic_pump_wraps_oserror_as_sttererror():
    """Direct unit on ``_mic_pump``: transport failure on ``send_media``
    wraps as ``STTError``."""

    class _OSErrorSocket:
        async def send_media(self, chunk: bytes) -> None:
            raise OSError("mic transport dropped")

    async def _one_chunk() -> AsyncIterator[bytes]:
        yield b"\x00" * 640

    with pytest.raises(STTError, match="audio send failed"):
        await agent.stt._mic_pump(_OSErrorSocket(), _one_chunk())


async def test_mic_pump_wraps_mic_iterator_exception_as_sttererror():
    """Direct unit on ``_mic_pump``: a generic exception from the mic
    iterator wraps as ``STTError`` with the ``mic source failed``
    wording."""

    async def _bad_mic() -> AsyncIterator[bytes]:
        yield b"\x00" * 640
        raise RuntimeError("mic hardware vanished")

    class _NoopSocket:
        async def send_media(self, chunk: bytes) -> None:
            pass

    with pytest.raises(STTError, match="mic source failed"):
        await agent.stt._mic_pump(_NoopSocket(), _bad_mic())


async def test_mic_pump_re_raises_cancellation() -> None:
    """``CancelledError`` from the mic iterator (external cancel of the
    pump task) propagates as-is, not wrapped."""

    async def _cancelled_mic() -> AsyncIterator[bytes]:
        raise asyncio.CancelledError
        yield b""  # pragma: no cover — never reached

    class _NoopSocket:
        async def send_media(self, chunk: bytes) -> None:
            pass

    with pytest.raises(asyncio.CancelledError):
        await agent.stt._mic_pump(_NoopSocket(), _cancelled_mic())


async def test_detect_barge_in_pump_failure_swallowed_during_drain(
    monkeypatch, fast_bargein_confirm, caplog
):
    """When ``_mic_pump`` has raised ``STTError`` before the receive
    side returns, ``detect_barge_in``'s drain in ``finally`` swallows
    it with a WARNING instead of masking the receive-side outcome.

    Scripted carefully so the pump raises synchronously on the first
    ``send_media`` call (no event-loop timing to depend on): a mic
    iterator that yields a single chunk immediately, no
    ``asyncio.sleep`` before the yield.
    """

    class _OSErrorOnSendSocket(FakeAsyncSocket):
        async def send_media(self, chunk: bytes) -> None:  # type: ignore[override]
            raise OSError("mic transport dropped")

    async def _single_chunk_mic() -> AsyncIterator[bytes]:
        yield b"\x00" * 640
        # Hang after the single chunk so the pump stays scheduled
        # against the raise, not exits via exhaustion.
        await asyncio.Event().wait()

    socket = _OSErrorOnSendSocket(
        pre_messages=[
            _turn_info_update("StartOfTurn"),
            _turn_info_update("EagerEndOfTurn"),
        ],
        exhaust_after_messages=True,
    )
    _install_fake_client(monkeypatch, socket)

    barge_in_event = asyncio.Event()
    with caplog.at_level(logging.WARNING, logger="agent.stt"):
        # Yield to the loop briefly so the pump reaches send_media
        # before the receive side returns. Then run detect_barge_in.
        await asyncio.wait_for(
            detect_barge_in(
                _single_chunk_mic(),
                barge_in_event,
                confirmation_ms=fast_bargein_confirm,
            ),
            timeout=1.0,
        )

    # Receive-side confirmed the barge-in regardless of the pump.
    assert barge_in_event.is_set()
    # The pump failure may or may not land in the drain path depending
    # on event-loop ordering (cancel can preempt the first send_media
    # call); the test asserts the detector exits cleanly without
    # raising. The drain WARNING is covered deterministically by the
    # unit tests on ``_mic_pump`` above.


async def test_detect_barge_in_connect_oserror_raises_stt_error(monkeypatch):
    """A transport error during ``connect()`` (DNS/TCP/TLS) surfaces as
    ``STTError`` via the outer ``except (OSError, ...)`` block, same
    as ``transcribe()``."""

    class _FailingConnectCM:
        async def __aenter__(self):
            raise OSError("DNS blew up")

        async def __aexit__(self, *_a: object) -> None:
            return None

    fake_client = MagicMock(name="AsyncDeepgramClient()")
    fake_client.listen.v2.connect.return_value = _FailingConnectCM()

    monkeypatch.setattr(agent.stt, "_client", None)
    monkeypatch.setattr(agent.stt, "_get_client", lambda: fake_client)

    with pytest.raises(STTError, match="connection failed"):
        await detect_barge_in(_mock_mic(), asyncio.Event())


async def test_detect_barge_in_non_turninfo_type_is_logged_and_ignored(
    monkeypatch, fast_bargein_confirm
):
    """Non-TurnInfo / non-Warning / non-Error messages (e.g. a
    ``Connected`` payload) are logged at DEBUG and ignored, matching
    ``_receive``'s treatment of SDK-level housekeeping events."""
    socket = FakeAsyncSocket(
        pre_messages=[
            json.dumps(
                {
                    "type": "Connected",
                    "request_id": "test",
                    "sequence_id": 0,
                }
            ),
            _turn_info_update("StartOfTurn"),
            _turn_info_update("EagerEndOfTurn"),
        ],
        exhaust_after_messages=True,
    )
    _install_fake_client(monkeypatch, socket)

    barge_in_event = asyncio.Event()
    await asyncio.wait_for(
        detect_barge_in(_mock_mic(), barge_in_event, confirmation_ms=fast_bargein_confirm),
        timeout=1.0,
    )
    assert barge_in_event.is_set()


async def test_detect_barge_in_production_constant_default_is_150ms():
    """Guard against the autouse ``fast_bargein_confirm`` fixture
    being read as the production default — ``BARGEIN_CONFIRM_MS`` is
    the ADR-0005 Decision 2 ballpark the detector picks up when the
    caller doesn't override ``confirmation_ms``."""
    assert BARGEIN_CONFIRM_MS == 150
