"""Async audio I/O foundation over sounddevice.

Per ADR-0003 §Audio I/O: this is the ONLY module in the codebase that
imports ``sounddevice``. Everything else (STT capture in #50, TTS
playback in #51) goes through the public API here — ``capture``,
``playback``, ``list_devices``.

Sample rates are deliberately split between input (16 kHz for Deepgram
Flux) and output (24 kHz for ElevenLabs streaming) per ADR-0003; do not
collapse into a single constant. Resampling either direction is
pointless — the providers are speech-optimized at these native rates.

sounddevice is a thin Python wrapper over PortAudio's C API. PortAudio
invokes audio-thread callbacks that must never touch the asyncio event
loop directly — this module bridges via ``loop.call_soon_threadsafe``
(capture) and a lock-guarded ``bytearray`` (playback). The same pattern
(sync callback → asyncio queue) will be reused by #49 (pynput PTT) and
#51 (ElevenLabs streaming).
"""

from __future__ import annotations

import asyncio
import math
import struct
import sys
import threading
from collections.abc import AsyncIterator

import sounddevice as sd

__all__ = [
    "CAPTURE_QUEUE_MAX_CHUNKS",
    "INPUT_CHANNELS",
    "INPUT_CHUNK_BYTES",
    "INPUT_CHUNK_MS",
    "INPUT_CHUNK_SAMPLES",
    "INPUT_DTYPE",
    "INPUT_SAMPLE_RATE",
    "OUTPUT_CHANNELS",
    "OUTPUT_DTYPE",
    "OUTPUT_FRAME_BYTES",
    "OUTPUT_SAMPLE_RATE",
    "PLAYBACK_QUEUE_MAX_CHUNKS",
    "capture",
    "list_devices",
    "playback",
]

# --- STT ingest ---------------------------------------------------------
# Deepgram Flux is speech-optimized at 16 kHz mono s16le (ADR-0003).
INPUT_SAMPLE_RATE = 16000
INPUT_CHANNELS = 1
INPUT_DTYPE = "int16"
INPUT_CHUNK_MS = 20
# 20 ms @ 16 kHz mono s16le = 320 samples = 640 bytes.
# Deepgram accepts 20-250 ms per chunk; 20 ms minimizes latency for
# voice-agent responsiveness.
INPUT_CHUNK_SAMPLES = INPUT_SAMPLE_RATE * INPUT_CHUNK_MS // 1000  # 320
INPUT_CHUNK_BYTES = INPUT_CHUNK_SAMPLES * 2  # 640 (2 bytes per s16 sample)

# --- TTS output ---------------------------------------------------------
# ElevenLabs streaming default is 24 kHz mono s16le (ADR-0003).
OUTPUT_SAMPLE_RATE = 24000
OUTPUT_CHANNELS = 1
OUTPUT_DTYPE = "int16"
# s16le = 2 bytes per sample. Playback callback must emit complete
# frames to avoid corrupting sample boundaries when chunks aren't
# sample-aligned.
OUTPUT_FRAME_BYTES = OUTPUT_CHANNELS * 2

# --- Real-time buffering (ADR-0003 §Streaming end-to-end) ---------------
# Bound both directions on a latency budget. Capture uses drop-oldest
# because stale mic audio has no value in a real-time loop. Playback
# uses backpressure because dropped TTS chunks produce audible glitches.
CAPTURE_QUEUE_MAX_CHUNKS = 25  # 500 ms at INPUT_CHUNK_MS=20
PLAYBACK_QUEUE_MAX_CHUNKS = 96  # ~2 s at typical ElevenLabs chunk size
# Sync-side byte buffer high-water. The pump stops draining chunks from
# the asyncio queue when the byte buffer hits this. Together with the
# bounded asyncio queue this bounds total in-flight audio to roughly
# high-water + queue-worth-of-chunks, and propagates backpressure to
# the producer via `await queue.put()`.
_PLAYBACK_BUFFER_HIGH_WATER_BYTES = OUTPUT_SAMPLE_RATE * OUTPUT_FRAME_BYTES  # ~1 s


async def capture(device: int | None = None) -> AsyncIterator[bytes]:
    """Yield ~20 ms PCM chunks from an input device (or default when None).

    Runs until the consuming task is cancelled. The PortAudio stream is
    closed cleanly in a ``finally`` block regardless of exit path. No
    stop-event parameter — cancellation is the mechanism, matching
    ADR-0003's asyncio-first shape.

    When the consumer falls behind, oldest queued chunks are dropped
    rather than backpressuring the PortAudio callback (stale real-time
    audio has no value). Queue depth is bounded at
    ``CAPTURE_QUEUE_MAX_CHUNKS`` (~500 ms).

    Args:
        device: sounddevice input device index, or ``None`` for default.

    Yields:
        Raw PCM bytes: ``INPUT_CHUNK_BYTES`` (640) per chunk in the
        common case, ~20 ms apart at ``INPUT_SAMPLE_RATE`` (16 000 Hz),
        mono, s16le.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=CAPTURE_QUEUE_MAX_CHUNKS)

    def _enqueue(chunk: bytes) -> None:
        # Runs on the event loop (scheduled via call_soon_threadsafe).
        # Safe to touch the asyncio queue here.
        if queue.full():
            try:
                queue.get_nowait()  # drop oldest; stale real-time audio is worthless
            except asyncio.QueueEmpty:
                pass  # race with consumer; harmless
        queue.put_nowait(chunk)

    def _on_input(indata, frames, time_info, status) -> None:  # PortAudio thread
        # ``indata`` is a CFFI buffer owned by PortAudio; copy to bytes
        # so downstream can own it after the callback returns.
        loop.call_soon_threadsafe(_enqueue, bytes(indata))

    stream = sd.RawInputStream(
        samplerate=INPUT_SAMPLE_RATE,
        channels=INPUT_CHANNELS,
        dtype=INPUT_DTYPE,
        blocksize=INPUT_CHUNK_SAMPLES,
        device=device,
        callback=_on_input,
    )
    try:
        stream.start()
        while True:
            yield await queue.get()
    finally:
        stream.stop()
        stream.close()


async def playback(chunks: AsyncIterator[bytes], device: int | None = None) -> None:
    """Play streamed PCM chunks to an output device (or default when None).

    Returns when the input iterator is exhausted AND the playback buffer
    has drained. Cancellation aborts playback and closes the stream in a
    ``finally`` block. Assumes chunks are s16le mono at
    ``OUTPUT_SAMPLE_RATE`` (24 000 Hz) — no resampling.

    Chunks may be of any byte length; sample-frame alignment across
    chunk boundaries is handled internally (dangling sub-frame bytes
    are retained until the next chunk completes them).

    Backpressure: chunks flow producer → bounded ``asyncio.Queue``
    (``PLAYBACK_QUEUE_MAX_CHUNKS``) → pump task → sync byte buffer read
    by the PortAudio callback. The pump stops draining the queue once
    the byte buffer reaches ``_PLAYBACK_BUFFER_HIGH_WATER_BYTES``
    (~1 s of audio), which fills the queue and causes the producer's
    ``await queue.put()`` to block. Total in-flight audio is bounded
    on that path; the producer never gets arbitrarily far ahead of the
    speaker.

    Args:
        chunks: async iterator yielding raw PCM bytes (any chunk size).
        device: sounddevice output device index, or ``None`` for default.
    """
    queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=PLAYBACK_QUEUE_MAX_CHUNKS)
    buffer = bytearray()
    lock = threading.Lock()

    def _on_output(outdata, frames, time_info, status) -> None:  # PortAudio thread
        need = len(outdata)
        with lock:
            # Consume only complete sample frames; a dangling sub-frame
            # byte (odd length under s16le mono) stays in the buffer
            # for the next callback to complete. Without this the
            # dangling byte would be paired with a zero pad, splitting
            # what should have been one 16-bit sample across two.
            available_aligned = len(buffer) - (len(buffer) % OUTPUT_FRAME_BYTES)
            have = min(need, available_aligned)
            outdata[:have] = bytes(buffer[:have])
            del buffer[:have]
        if have < need:
            # Genuine underrun: fill remainder with silence. Producer is
            # either behind or done; the drain loop below exits once the
            # buffer is empty (dangling sub-frame bytes at end-of-stream
            # are discarded as truncated PCM — logging left to #55).
            outdata[have:] = bytes(need - have)

    stream = sd.RawOutputStream(
        samplerate=OUTPUT_SAMPLE_RATE,
        channels=OUTPUT_CHANNELS,
        dtype=OUTPUT_DTYPE,
        device=device,
        callback=_on_output,
    )

    async def _pump() -> None:
        # Move chunks from the async queue into the sync byte buffer.
        # Gated by _PLAYBACK_BUFFER_HIGH_WATER_BYTES so backpressure
        # actually reaches the producer: when the byte buffer is full,
        # the pump stops draining the queue, the queue fills, and the
        # producer's `await queue.put(chunk)` blocks.
        while True:
            chunk = await queue.get()
            if chunk is None:  # sentinel: producer finished
                return
            while True:
                with lock:
                    if len(buffer) < _PLAYBACK_BUFFER_HIGH_WATER_BYTES:
                        buffer.extend(chunk)
                        break
                await asyncio.sleep(0.02)

    pump_task: asyncio.Task[None] | None = None
    try:
        stream.start()
        pump_task = asyncio.create_task(_pump())
        async for chunk in chunks:
            await queue.put(chunk)  # bounded — awaits when full (backpressure)
        await queue.put(None)  # sentinel; pump exits after processing
        await pump_task
        # Producer + pump done — wait for the callback to drain the buffer.
        # 20 ms poll interval balances wake-ups vs. tail latency.
        while True:
            with lock:
                if not buffer:
                    break
            await asyncio.sleep(0.02)
    finally:
        # Cancel the pump if we bailed out before it finished (mid-flight
        # exception or cancellation of the caller).
        if pump_task is not None and not pump_task.done():
            pump_task.cancel()
            try:
                await pump_task
            except asyncio.CancelledError:
                pass
        stream.stop()
        stream.close()


def list_devices() -> str:
    """Return a human-readable inventory of audio devices.

    Thin wrapper over ``sounddevice.query_devices()`` so setup and
    debugging can be invoked from ``python -m agent.audio devices``
    without opening any streams. Safe on systems with no audio devices —
    sounddevice returns a "No devices found" string rather than raising.
    """
    return str(sd.query_devices())


# --- __main__ helpers ---------------------------------------------------
# The ``if __name__ == "__main__":`` dispatch below carries a
# ``# pragma: no cover`` — CLI dispatch (argv parsing + subcommand
# branching) is exercised by the manual acceptance test, not by pytest.
# This is the ONLY use of ``pragma: no cover`` in the codebase and sets
# a project precedent: reserved for CLI dispatch that cannot be
# meaningfully tested without subprocesses, not a general escape hatch.
# The dispatch's helpers (``_run_test``, ``_sine_pcm``, ``_iter_bytes``)
# stay covered by unit tests with mocked ``capture``/``playback``.


def _sine_pcm(freq_hz: float, seconds: float, sample_rate: int) -> bytes:
    """Generate s16le mono PCM of a sine tone.

    Pure stdlib (``math`` + ``struct``); no numpy. Used by the ``test``
    CLI subcommand to prove ``playback`` works end-to-end with real
    hardware at ``OUTPUT_SAMPLE_RATE``.
    """
    n = int(seconds * sample_rate)
    amp = 8000  # ~25% of int16 max — audible but not painful
    samples = [int(amp * math.sin(2 * math.pi * freq_hz * i / sample_rate)) for i in range(n)]
    return struct.pack(f"<{n}h", *samples)


async def _iter_bytes(data: bytes, chunk_size: int = 4800) -> AsyncIterator[bytes]:
    """Feed a bytes buffer into ``playback`` in fixed-size chunks."""
    for i in range(0, len(data), chunk_size):
        yield data[i : i + chunk_size]


async def _run_test(record_seconds: float = 3.0) -> None:
    """Manual acceptance test: capture from mic, then play a tone.

    Two-part verification because ``capture`` and ``playback`` use
    different provider-native sample rates (16 kHz for STT, 24 kHz for
    TTS). Playing recorded mic input through ``playback`` would run at
    the wrong rate; instead this test verifies capture by byte count
    and verifies playback by synthesizing a known-good 24 kHz tone.

    Timing note: the 3 s window starts when the first audio chunk
    arrives, not when the stream is constructed. PortAudio streams on
    Windows have ~200-800 ms warm-up latency that would otherwise eat
    into the timed window and drop the byte count below tolerance.
    """
    print(
        f"[capture] recording {record_seconds:.0f}s from default mic "
        f"({INPUT_SAMPLE_RATE} Hz mono s16le)..."
    )
    recorded: list[bytes] = []

    async def _collect() -> None:
        async for chunk in capture():
            recorded.append(chunk)

    task = asyncio.create_task(_collect())
    try:
        # Wait for the mic stream to warm up (first chunk in hand).
        # 5 s ceiling catches "mic not available" without hanging.
        try:
            await asyncio.wait_for(_await_first_chunk(recorded), timeout=5.0)
        except TimeoutError:
            print("[capture] FAIL: no audio in 5s (mic not available?)", file=sys.stderr)
            sys.exit(1)

        # Mic is live — now time the actual recording window.
        await asyncio.sleep(record_seconds)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    got = sum(len(c) for c in recorded)
    expect = int(record_seconds * INPUT_SAMPLE_RATE) * 2  # 2 bytes/sample
    print(f"[capture] {got} bytes captured (expected ~{expect}, ±15% tolerance)")
    if not (expect * 0.85 <= got <= expect * 1.15):
        print("[capture] FAIL: byte count outside tolerance", file=sys.stderr)
        sys.exit(1)

    print(f"[playback] playing a 1s 440 Hz tone at {OUTPUT_SAMPLE_RATE} Hz mono s16le...")
    tone = _sine_pcm(freq_hz=440.0, seconds=1.0, sample_rate=OUTPUT_SAMPLE_RATE)
    await playback(_iter_bytes(tone))
    print("OK")


async def _await_first_chunk(recorded: list[bytes]) -> None:
    """Poll ``recorded`` at 10 ms intervals until it holds at least one chunk."""
    while not recorded:
        await asyncio.sleep(0.01)


if __name__ == "__main__":  # pragma: no cover
    argv = sys.argv[1:]
    if len(argv) == 1 and argv[0] == "devices":
        print(list_devices())
    elif len(argv) == 1 and argv[0] == "test":
        asyncio.run(_run_test())
    else:
        print("usage: python -m agent.audio {devices|test}", file=sys.stderr)
        sys.exit(2)
