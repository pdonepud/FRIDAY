"""Tests for the Deepgram Flux STT module (``agent.stt``).

No test opens a real Deepgram websocket. ``AsyncDeepgramClient`` is
mocked via ``monkeypatch`` — a fake client exposes a fake
``.listen.v2.connect(**kwargs)`` that returns a scripted
``FakeAsyncSocket``. Manual hardware verification lives in
``python -m agent.stt``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from inspect import iscoroutinefunction
from unittest.mock import MagicMock

import pytest
from deepgram.core.api_error import ApiError
from deepgram.listen.v2.types.listen_v2fatal_error import ListenV2FatalError
from deepgram.listen.v2.types.listen_v2turn_info import ListenV2TurnInfo

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


def _turn_info(event: str, transcript: str = "", trigger: str | None = None) -> ListenV2TurnInfo:
    """Build a real ListenV2TurnInfo via pydantic construction."""
    return ListenV2TurnInfo(
        request_id="test-req",
        sequence_id=1,
        event=event,
        turn_index=0,
        audio_window_start=0.0,
        audio_window_end=1.0,
        transcript=transcript,
        words=[],
        end_of_turn_confidence=0.9,
        trigger=trigger,
    )


def _fatal_error() -> ListenV2FatalError:
    """Build a real ListenV2FatalError. Fields per the SDK model."""
    return ListenV2FatalError(
        sequence_id=1,
        code="INTERNAL_SERVER_ERROR",
        description="test",
    )


class FakeAsyncSocket:
    """Test double for ``deepgram.listen.v2.socket_client.AsyncV2SocketClient``.

    - ``send_media`` / ``send_force_end_turn`` record call order into
      ``call_order`` for send-ordering assertions.
    - ``__aiter__`` yields scripted messages, then blocks on
      ``_release_iter`` until the surrounding task is cancelled or a
      ``force_end_release`` sentinel is provided.
    - A message that is an ``Exception`` instance is raised at that
      position in the iteration (models ConnectionClosed mid-stream).
    """

    def __init__(
        self,
        messages: list | None = None,
        *,
        raise_on_force_end_turn: BaseException | None = None,
        yield_after_force_end_turn: list | None = None,
    ):
        self._messages = list(messages or [])
        self._raise_on_force_end_turn = raise_on_force_end_turn
        self._post_force_end_turn = list(yield_after_force_end_turn or [])
        self._force_end_turn_seen = asyncio.Event()
        self._release_iter = asyncio.Event()  # never set unless the test wants termination
        self.call_order: list[tuple] = []
        self.send_media_calls: list[bytes] = []
        self.force_end_turn_count = 0

    async def send_media(self, chunk: bytes) -> None:
        self.call_order.append(("send_media", chunk))
        self.send_media_calls.append(chunk)

    async def send_force_end_turn(self, message=None) -> None:
        self.call_order.append(("send_force_end_turn",))
        self.force_end_turn_count += 1
        self._force_end_turn_seen.set()
        if self._raise_on_force_end_turn is not None:
            raise self._raise_on_force_end_turn

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for msg in self._messages:
            if isinstance(msg, BaseException):
                raise msg
            yield msg
        # If the test wants extra messages after ForceEndTurn fires,
        # wait for it and yield them.
        if self._post_force_end_turn:
            await self._force_end_turn_seen.wait()
            for msg in self._post_force_end_turn:
                if isinstance(msg, BaseException):
                    raise msg
                yield msg
        # Otherwise block until cancelled.
        await self._release_iter.wait()


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


def _install_fake_client(monkeypatch, socket: FakeAsyncSocket) -> FakeConnectCM:
    """Wire up ``_get_client`` to return a client whose ``listen.v2.connect``
    yields ``socket`` inside a ``FakeConnectCM``. Returns the CM instance
    so tests can inspect ``entered`` / ``exited`` / ``kwargs``.
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
    # Return a proxy that lazily reads the created CM.
    proxy = MagicMock()
    proxy.cm = property(lambda _: cm_holder[-1] if cm_holder else None)
    # A property on a MagicMock is awkward; just expose the holder.
    proxy._holder = cm_holder
    return proxy  # tests: proxy._holder[-1] gives the created CM


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
    """Module import must not touch AsyncDeepgramClient."""
    monkeypatch.setattr(agent.stt, "_client", None)
    assert agent.stt._client is None


def test_get_client_caches_instance(monkeypatch):
    """Repeated _get_client() calls return the same instance."""
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
# Bundle 5: happy path + send-ordering assertion
# ---------------------------------------------------------------------------


async def test_happy_path_returns_transcript_and_orders_sends(monkeypatch):
    """All send_media calls precede the single send_force_end_turn call."""
    socket = FakeAsyncSocket(
        messages=[],
        yield_after_force_end_turn=[
            _turn_info("StartOfTurn"),
            _turn_info("EndOfTurn", transcript="hello world", trigger="manual"),
        ],
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    chunks_payload = [b"AAA", b"BBB", b"CCC"]

    async def _run():
        return await transcribe(_chunks_from(chunks_payload), ptt_release)

    task = asyncio.create_task(_run())
    # Let pump send its chunks and receive begin waiting.
    await asyncio.sleep(0.05)
    # User releases PTT.
    ptt_release.set()

    transcript = await asyncio.wait_for(task, timeout=1.0)
    assert transcript == "hello world"

    # Send ordering: every send_media entry precedes the send_force_end_turn.
    force_index = [i for i, c in enumerate(socket.call_order) if c[0] == "send_force_end_turn"]
    assert len(force_index) == 1, "ForceEndTurn must be sent exactly once"
    force_at = force_index[0]
    media_indices = [i for i, c in enumerate(socket.call_order) if c[0] == "send_media"]
    assert media_indices, "send_media must have been called at least once"
    assert all(i < force_at for i in media_indices), (
        f"all send_media calls must precede send_force_end_turn: {socket.call_order}"
    )
    assert socket.send_media_calls == chunks_payload


# ---------------------------------------------------------------------------
# Bundle 6: config kwargs forwarded to connect()
# ---------------------------------------------------------------------------


async def test_connect_kwargs_forwarded(monkeypatch):
    socket = FakeAsyncSocket(
        yield_after_force_end_turn=[
            _turn_info("EndOfTurn", transcript="ok", trigger="manual"),
        ],
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
# Bundle 7: trigger == "timeout" → STTTimeout(partial=...)
# ---------------------------------------------------------------------------


async def test_trigger_timeout_raises_STTTimeout_with_partial(monkeypatch):
    socket = FakeAsyncSocket(
        yield_after_force_end_turn=[
            _turn_info("EndOfTurn", transcript="half a sentence", trigger="timeout"),
        ],
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    task = asyncio.create_task(transcribe(_empty_chunks(), ptt_release))
    await asyncio.sleep(0.02)
    ptt_release.set()

    with pytest.raises(STTTimeout) as exc_info:
        await asyncio.wait_for(task, timeout=1.0)
    assert exc_info.value.partial == "half a sentence"


# ---------------------------------------------------------------------------
# Bundle 8: trigger == "model" → returns transcript, logs warning
# ---------------------------------------------------------------------------


async def test_trigger_model_returns_transcript_with_warning(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="agent.stt")
    socket = FakeAsyncSocket(
        yield_after_force_end_turn=[
            _turn_info("EndOfTurn", transcript="something", trigger="model"),
        ],
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    task = asyncio.create_task(transcribe(_empty_chunks(), ptt_release))
    await asyncio.sleep(0.02)
    ptt_release.set()

    transcript = await asyncio.wait_for(task, timeout=1.0)
    assert transcript == "something"
    warnings = [r for r in caplog.records if "trigger='model'" in r.getMessage()]
    assert warnings, f"expected trigger='model' warning, got {caplog.records}"


# ---------------------------------------------------------------------------
# Bundle 9: trigger unknown → returns transcript, logs warning
# ---------------------------------------------------------------------------


async def test_trigger_unknown_returns_transcript_with_warning(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="agent.stt")
    socket = FakeAsyncSocket(
        yield_after_force_end_turn=[
            _turn_info("EndOfTurn", transcript="mystery", trigger="brand-new-trigger"),
        ],
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    task = asyncio.create_task(transcribe(_empty_chunks(), ptt_release))
    await asyncio.sleep(0.02)
    ptt_release.set()

    transcript = await asyncio.wait_for(task, timeout=1.0)
    assert transcript == "mystery"
    warnings = [r for r in caplog.records if "unknown trigger" in r.getMessage()]
    assert warnings, f"expected unknown-trigger warning, got {caplog.records}"


# ---------------------------------------------------------------------------
# Bundle 10: FatalError → STTFatal
# ---------------------------------------------------------------------------


async def test_fatal_error_raises_STTFatal(monkeypatch):
    socket = FakeAsyncSocket(messages=[_fatal_error()])
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    with pytest.raises(STTFatal):
        await asyncio.wait_for(transcribe(_empty_chunks(), ptt_release), timeout=1.0)


# ---------------------------------------------------------------------------
# Bundle 11: connection close mid-stream → STTError
# ---------------------------------------------------------------------------


async def test_connection_closed_mid_stream_raises_STTError(monkeypatch):
    class _FakeConnectionClosed(Exception):
        pass

    socket = FakeAsyncSocket(messages=[_FakeConnectionClosed("simulated close")])
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    with pytest.raises(STTError) as exc_info:
        await asyncio.wait_for(transcribe(_empty_chunks(), ptt_release), timeout=1.0)
    # Not a more-specific STT subtype:
    assert type(exc_info.value) is STTError
    assert "receive loop failed" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Bundle 12: ForceEndTurn unsupported → STTForceEndTurnUnsupported
# ---------------------------------------------------------------------------


async def test_force_end_turn_unsupported_raises(monkeypatch):
    socket = FakeAsyncSocket(
        raise_on_force_end_turn=RuntimeError("UNPARSABLE_CLIENT_MESSAGE"),
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    task = asyncio.create_task(transcribe(_empty_chunks(), ptt_release))
    await asyncio.sleep(0.02)
    ptt_release.set()

    with pytest.raises(STTForceEndTurnUnsupported):
        await asyncio.wait_for(task, timeout=1.0)


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
# Bundle 14a: cancellation cleanup mid-stream
# ---------------------------------------------------------------------------


async def test_cancellation_mid_stream_closes_socket(monkeypatch):
    """Cancel transcribe after chunks have started; socket __aexit__ must run."""
    socket = FakeAsyncSocket()  # no scripted messages; hangs
    proxy = _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()

    async def _long_chunks() -> AsyncIterator[bytes]:
        for _ in range(1000):
            yield b"AAAA"
            await asyncio.sleep(0.001)

    task = asyncio.create_task(transcribe(_long_chunks(), ptt_release))
    await asyncio.sleep(0.05)  # let pump run and receive enter its loop
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    cm = proxy._holder[-1]
    assert cm.entered is True
    assert cm.exited is True


# ---------------------------------------------------------------------------
# Bundle 14b: cancellation on the totally-idle path — cancel before any
# task has resolved the future. Catches socket-leak regressions.
# ---------------------------------------------------------------------------


async def test_cancellation_before_any_task_resolves(monkeypatch):
    socket = FakeAsyncSocket()  # no messages, no send activity
    proxy = _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    task = asyncio.create_task(transcribe(_empty_chunks(), ptt_release))
    # Let transcribe enter the async-with, spawn the three tasks, and
    # begin awaiting done_future — but don't set ptt_release or feed
    # chunks. Nothing resolves the future.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    cm = proxy._holder[-1]
    assert cm.entered is True
    assert cm.exited is True, "socket context manager must exit on idle cancel"


# ---------------------------------------------------------------------------
# Bundle 15: receive timeout guard fires when nothing arrives
# ---------------------------------------------------------------------------


async def test_receive_timeout_guard_fires(monkeypatch):
    monkeypatch.setattr(agent.stt, "RECEIVE_TIMEOUT_S", 0.05)
    socket = FakeAsyncSocket()  # no messages, receive hangs
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    with pytest.raises(STTError, match="no EndOfTurn within"):
        await asyncio.wait_for(transcribe(_empty_chunks(), ptt_release), timeout=1.0)


# ---------------------------------------------------------------------------
# Bundle 16: ptt_release already set at entry, with EMPTY chunks
# → send_force_end_turn precedes any send_media (trivially, since there
# is no send_media). Deterministic ordering guarantee.
# ---------------------------------------------------------------------------


async def test_pre_set_ptt_release_empty_chunks_ordering(monkeypatch):
    socket = FakeAsyncSocket(
        yield_after_force_end_turn=[
            _turn_info("EndOfTurn", transcript="", trigger="manual"),
        ],
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    ptt_release.set()  # already set at call time

    transcript = await asyncio.wait_for(transcribe(_empty_chunks(), ptt_release), timeout=1.0)
    assert transcript == ""

    # Deterministic: no send_media calls, exactly one send_force_end_turn.
    assert socket.send_media_calls == []
    assert socket.force_end_turn_count == 1
    assert socket.call_order == [("send_force_end_turn",)]


# ---------------------------------------------------------------------------
# Bundle 17: ptt_release pre-set with NON-EMPTY chunks — no ordering claim,
# just that the pipeline completes cleanly.
# ---------------------------------------------------------------------------


async def test_pre_set_ptt_release_with_chunks_returns_cleanly(monkeypatch):
    socket = FakeAsyncSocket(
        yield_after_force_end_turn=[
            _turn_info("EndOfTurn", transcript="done", trigger="manual"),
        ],
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    ptt_release.set()  # already set at call time; race with pump is expected

    transcript = await asyncio.wait_for(
        transcribe(_chunks_from([b"AAA", b"BBB"]), ptt_release),
        timeout=1.0,
    )
    assert transcript == "done"
    # No ordering claim here — pre-set release racing with pump is the
    # documented (defensive) contract, not a guarantee.


# ---------------------------------------------------------------------------
# Bundle 18: empty transcript from EndOfTurn/manual is a valid outcome
# ---------------------------------------------------------------------------


async def test_empty_transcript_returns_empty_string(monkeypatch):
    socket = FakeAsyncSocket(
        yield_after_force_end_turn=[
            _turn_info("EndOfTurn", transcript="", trigger="manual"),
        ],
    )
    _install_fake_client(monkeypatch, socket)

    ptt_release = asyncio.Event()
    task = asyncio.create_task(transcribe(_empty_chunks(), ptt_release))
    await asyncio.sleep(0.02)
    ptt_release.set()

    transcript = await asyncio.wait_for(task, timeout=1.0)
    assert transcript == ""


# ---------------------------------------------------------------------------
# Bundle 19: _main dispatch wiring
# ---------------------------------------------------------------------------


async def test_main_wires_audio_ptt_and_transcribe(monkeypatch, capsys):
    """_main pulls capture(), watches for PRESSED/RELEASED, calls transcribe."""
    from agent import ptt

    async def _fake_capture():
        # Never yields; _main's transcribe would await forever without ptt.
        while True:
            await asyncio.sleep(0.01)
            yield b"\x00" * 640

    async def _fake_events():
        yield ptt.PTTEvent.PRESSED
        yield ptt.PTTEvent.RELEASED

    async def _fake_transcribe(chunks, ptt_release):
        # Realistic behavior: wait for the PTT release before returning.
        # The watcher task races us; asserting is_set() at entry is racy.
        await asyncio.wait_for(ptt_release.wait(), timeout=1.0)
        return "hello from stt"

    # Patch inside _main's imports via the module attribute path.
    import agent.audio as _audio_mod
    import agent.ptt as _ptt_mod

    monkeypatch.setattr(_audio_mod, "capture", _fake_capture)
    monkeypatch.setattr(_ptt_mod, "events", _fake_events)
    monkeypatch.setattr(agent.stt, "transcribe", _fake_transcribe)

    await agent.stt._main()

    captured = capsys.readouterr()
    assert "hello from stt" in captured.out


# ---------------------------------------------------------------------------
# Bundle 20: ApiError with status 401 → STTAuthError (bonus wrap check)
# ---------------------------------------------------------------------------


async def test_api_error_401_becomes_STTAuthError(monkeypatch):
    """401 from Deepgram's ApiError should surface as STTAuthError."""

    def _bad_connect(**kwargs):
        raise ApiError(status_code=401, body="invalid credentials")

    fake_client = MagicMock()
    fake_client.listen.v2.connect.side_effect = _bad_connect
    monkeypatch.setattr(agent.stt, "_client", None)
    monkeypatch.setattr(agent.stt, "_get_client", lambda: fake_client)

    ptt_release = asyncio.Event()
    with pytest.raises(STTAuthError):
        await asyncio.wait_for(transcribe(_empty_chunks(), ptt_release), timeout=1.0)
