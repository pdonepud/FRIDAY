"""Tests for the async audio I/O module (``agent.audio``).

No test opens a real PortAudio stream — all hardware paths are mocked
via ``monkeypatch`` against ``agent.audio.sd``. Manual hardware
verification lives in ``python -m agent.audio test`` (see ADR-0003 and
the ``_run_test`` helper).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import threading
import time
from collections.abc import AsyncIterator

import pytest

import agent.audio
from agent.audio import (
    INPUT_CHANNELS,
    INPUT_CHUNK_BYTES,
    INPUT_CHUNK_MS,
    INPUT_CHUNK_SAMPLES,
    INPUT_DTYPE,
    INPUT_SAMPLE_RATE,
    OUTPUT_CHANNELS,
    OUTPUT_DTYPE,
    OUTPUT_SAMPLE_RATE,
    capture,
    list_devices,
    playback,
)

# --- Bundle 1: constants sanity -----------------------------------------


def test_input_constants_have_expected_values():
    """Deepgram Flux ingest is locked to 16 kHz mono s16le, 20 ms chunks."""
    assert INPUT_SAMPLE_RATE == 16000
    assert INPUT_CHANNELS == 1
    assert INPUT_DTYPE == "int16"
    assert INPUT_CHUNK_MS == 20
    assert INPUT_CHUNK_SAMPLES == 320
    assert INPUT_CHUNK_BYTES == 640


def test_output_constants_have_expected_values():
    """ElevenLabs streaming default is 24 kHz mono s16le."""
    assert OUTPUT_SAMPLE_RATE == 24000
    assert OUTPUT_CHANNELS == 1
    assert OUTPUT_DTYPE == "int16"


def test_all_size_constants_are_positive_ints():
    for value in (
        INPUT_SAMPLE_RATE,
        INPUT_CHANNELS,
        INPUT_CHUNK_MS,
        INPUT_CHUNK_SAMPLES,
        INPUT_CHUNK_BYTES,
        OUTPUT_SAMPLE_RATE,
        OUTPUT_CHANNELS,
    ):
        assert isinstance(value, int)
        assert value > 0


def test_input_chunk_bytes_is_samples_times_two():
    """Sample-to-byte derivation must stay consistent for s16le."""
    assert INPUT_CHUNK_BYTES == INPUT_CHUNK_SAMPLES * 2


# --- Bundle 2: list_devices ---------------------------------------------


def test_list_devices_returns_string():
    """Safe even without hardware — sounddevice returns a string in all cases."""
    result = list_devices()
    assert isinstance(result, str)


# --- Bundle 3: API shape ------------------------------------------------


def test_capture_is_async_generator_function():
    assert inspect.isasyncgenfunction(capture)


def test_playback_is_coroutine_function():
    assert inspect.iscoroutinefunction(playback)


def test_list_devices_is_plain_function():
    assert not inspect.iscoroutinefunction(list_devices)
    assert not inspect.isasyncgenfunction(list_devices)


# --- Fake PortAudio streams for bundles 4-6 -----------------------------


class _FakeInputStream:
    """Test double for ``sounddevice.RawInputStream``.

    On ``start()`` spawns a daemon thread that fires ``callback`` with a
    scripted sequence of PCM byte chunks. This exercises the real
    async-bridge path in ``capture()`` (callback in a background thread
    → ``loop.call_soon_threadsafe`` → asyncio queue) without any
    hardware.
    """

    def __init__(
        self,
        *,
        samplerate,
        channels,
        dtype,
        blocksize,
        device,
        callback,
    ):
        self.samplerate = samplerate
        self.channels = channels
        self.dtype = dtype
        self.blocksize = blocksize
        self.device = device
        self.callback = callback
        self.started = False
        self.stopped = False
        self.closed = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Scripted chunks (bytes) fed via callback:
        self.chunks_to_feed: list[bytes] = [
            b"AAAAA",
            b"BBBBB",
            b"CCCCC",
        ]

    def start(self):
        self.started = True

        def _feed():
            for chunk in self.chunks_to_feed:
                if self._stop.is_set():
                    return
                self.callback(chunk, len(chunk) // 2, None, None)
                time.sleep(0.001)
            # After scripted chunks, hang until stopped so consumer
            # cancellation paths can be exercised.
            self._stop.wait(timeout=5)

        self._thread = threading.Thread(target=_feed, daemon=True)
        self._thread.start()

    def stop(self):
        self.stopped = True
        self._stop.set()

    def close(self):
        self.closed = True
        if self._thread is not None:
            self._thread.join(timeout=1)


class _FakeOutputStream:
    """Test double for ``sounddevice.RawOutputStream``.

    On ``start()`` spawns a daemon thread that polls ``callback`` at
    ~5 ms intervals, mimicking PortAudio's audio-callback cadence. Bytes
    the callback writes into ``outdata`` are appended to ``.written``
    for assertion in tests.
    """

    def __init__(self, *, samplerate, channels, dtype, device, callback):
        self.samplerate = samplerate
        self.channels = channels
        self.dtype = dtype
        self.device = device
        self.callback = callback
        self.started = False
        self.stopped = False
        self.closed = False
        self.written = bytearray()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self):
        self.started = True
        buf = bytearray(1024)  # request 1024 bytes per callback

        def _pump():
            mv = memoryview(buf)
            while not self._stop.is_set():
                # Zero the buffer between polls so underrun-pad bytes
                # from the previous poll do not leak into this one.
                buf[:] = b"\x00" * len(buf)
                self.callback(mv, len(buf) // 2, None, None)
                self.written.extend(bytes(buf))
                time.sleep(0.005)

        self._thread = threading.Thread(target=_pump, daemon=True)
        self._thread.start()

    def stop(self):
        self.stopped = True
        self._stop.set()

    def close(self):
        self.closed = True
        if self._thread is not None:
            self._thread.join(timeout=1)


@pytest.fixture
def fake_input_stream(monkeypatch):
    """Install ``_FakeInputStream`` as ``agent.audio.sd.RawInputStream``.

    Returns a one-element list that receives the ``_FakeInputStream``
    instance once ``capture()`` constructs it. Tests use
    ``fake_input_stream[0]`` after driving the async generator.
    """
    captured: list[_FakeInputStream] = []

    def _factory(**kwargs):
        stream = _FakeInputStream(**kwargs)
        captured.append(stream)
        return stream

    monkeypatch.setattr(agent.audio.sd, "RawInputStream", _factory)
    return captured


@pytest.fixture
def fake_output_stream(monkeypatch):
    """Install ``_FakeOutputStream`` as ``agent.audio.sd.RawOutputStream``."""
    captured: list[_FakeOutputStream] = []

    def _factory(**kwargs):
        stream = _FakeOutputStream(**kwargs)
        captured.append(stream)
        return stream

    monkeypatch.setattr(agent.audio.sd, "RawOutputStream", _factory)
    return captured


# --- Bundle 4: async bridge behavior ------------------------------------


async def test_capture_yields_callback_bytes_in_order(fake_input_stream):
    """PortAudio callback bytes reach the async consumer via the queue bridge.

    Verifies the ``loop.call_soon_threadsafe(queue.put_nowait, ...)``
    seam works end-to-end: three scripted chunks pushed from a
    background thread arrive in the async generator in the same order
    they were fed.
    """
    gen = capture()
    received: list[bytes] = []
    async for chunk in gen:
        received.append(chunk)
        if len(received) == 3:
            break
    await gen.aclose()

    assert received == [b"AAAAA", b"BBBBB", b"CCCCC"]
    assert len(fake_input_stream) == 1
    stream = fake_input_stream[0]
    assert stream.started is True
    assert stream.blocksize == INPUT_CHUNK_SAMPLES
    assert stream.samplerate == INPUT_SAMPLE_RATE
    assert stream.channels == INPUT_CHANNELS
    assert stream.dtype == INPUT_DTYPE


async def test_capture_passes_device_argument(fake_input_stream):
    """``capture(device=N)`` forwards ``device`` to the stream factory."""
    gen = capture(device=7)
    async for _ in gen:
        break
    await gen.aclose()

    assert fake_input_stream[0].device == 7


# --- Bundle 5: cancellation cleanup -------------------------------------


async def test_capture_closes_stream_on_task_cancellation(fake_input_stream):
    """Cancelling the consumer task runs ``finally`` and closes the stream."""

    async def _consume():
        async for _ in capture():
            pass

    task = asyncio.create_task(_consume())
    # Let capture() install the stream and start yielding.
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert len(fake_input_stream) == 1
    stream = fake_input_stream[0]
    assert stream.stopped is True
    assert stream.closed is True


# --- Bundle 6: playback drains input + underrun padding -----------------


async def _yield_chunks(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for c in chunks:
        yield c


async def test_playback_drains_input_and_closes_stream(fake_output_stream):
    """Chunks pushed into ``playback`` end up in the callback's ``outdata``."""
    payload_a = b"AAAA" * 200  # 800 bytes
    payload_b = b"BBBB" * 200  # 800 bytes

    await playback(_yield_chunks([payload_a, payload_b]))

    assert len(fake_output_stream) == 1
    stream = fake_output_stream[0]
    assert stream.started is True
    assert stream.stopped is True
    assert stream.closed is True
    written = bytes(stream.written)
    # Payload bytes must have reached the callback (interleaved with
    # trailing zero pads once the buffer drained — accepted).
    assert b"AAAA" in written
    assert b"BBBB" in written
    assert stream.samplerate == OUTPUT_SAMPLE_RATE
    assert stream.channels == OUTPUT_CHANNELS
    assert stream.dtype == OUTPUT_DTYPE


async def test_playback_pads_underrun_with_zeros(fake_output_stream):
    """When the buffer is empty the callback fills ``outdata`` with zeros.

    Feeding no chunks at all means every callback poll pulls from an
    empty buffer and must pad the full request with silence.
    """

    async def _no_chunks() -> AsyncIterator[bytes]:
        # Empty generator — never yields.
        return
        yield  # pragma: no cover  (unreachable; keeps this a generator)

    await playback(_no_chunks())

    stream = fake_output_stream[0]
    assert stream.closed is True
    # All bytes the callback saw should be zero (padded silence).
    written = bytes(stream.written)
    # ``written`` may be empty if the callback never ran before the
    # drain loop noticed the empty buffer; either outcome is valid.
    assert all(b == 0 for b in written)


async def test_playback_passes_device_argument(fake_output_stream):
    """``playback(chunks, device=N)`` forwards ``device`` to the stream factory."""

    async def _empty() -> AsyncIterator[bytes]:
        return
        yield  # pragma: no cover

    await playback(_empty(), device=3)

    assert fake_output_stream[0].device == 3


# --- Bundle 7: _run_test wires capture and playback (dispatch helper) ---


async def test_run_test_helper_wires_capture_and_playback(monkeypatch):
    """The ``test`` CLI subcommand's helper calls ``capture`` then ``playback``.

    Proves the dispatch wiring without going near a subprocess. Fake
    ``capture`` yields exactly the tolerance-passing byte count for the
    short record window; fake ``playback`` drains the tone.
    """
    playback_called_with: list[AsyncIterator[bytes]] = []

    record_seconds = 0.05
    expect_bytes = int(record_seconds * INPUT_SAMPLE_RATE) * 2  # 1600

    async def _fake_capture():
        # First chunk satisfies the ±10% tolerance immediately.
        yield b"\x00" * expect_bytes
        # Then hang until the collector task is cancelled.
        while True:
            await asyncio.sleep(0.01)

    async def _fake_playback(chunks, device=None):
        playback_called_with.append(chunks)
        async for _ in chunks:
            pass

    monkeypatch.setattr(agent.audio, "capture", _fake_capture)
    monkeypatch.setattr(agent.audio, "playback", _fake_playback)

    await agent.audio._run_test(record_seconds=record_seconds)

    assert len(playback_called_with) == 1


def test_sine_pcm_returns_correct_byte_count():
    """``_sine_pcm`` returns ``seconds * sample_rate * 2`` bytes (s16le mono)."""
    out = agent.audio._sine_pcm(freq_hz=440.0, seconds=0.5, sample_rate=24000)
    assert len(out) == int(0.5 * 24000) * 2
