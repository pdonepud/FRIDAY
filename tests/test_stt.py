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


# ---------------------------------------------------------------------------
# Bundle 1: constants
# ---------------------------------------------------------------------------


def test_config_constants():
    assert MODEL == "flux-general-en"
    assert ENCODING == "linear16"
    assert SAMPLE_RATE == 16000
    assert EOT_THRESHOLD == 1.0
    assert EOT_TIMEOUT_MS == 30000
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
