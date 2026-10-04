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
import contextlib
import math
import struct
import sys
import threading
import time
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
    "AudioDeviceUnavailable",
    "AudioError",
    "AudioPlaybackError",
    "capture",
    "list_devices",
    "playback",
    "probe_input_device",
    "probe_output_device",
]


# --- Exception hierarchy (#55) ------------------------------------------
# Mirrors agent.stt.STTError's shape: a base class with narrow leaves
# so voice_loop can distinguish startup-probe failures (recoverable →
# fall back to text) from mid-stream playback failures (recoverable →
# log + continue, with the sentence text printed so the user still
# sees Claude's reply).


class AudioError(Exception):
    """Base class for errors surfaced by probes / capture / playback."""


class AudioDeviceUnavailable(AudioError):
    """A startup probe determined no usable default input/output device exists.

    Raised by ``probe_input_device`` / ``probe_output_device`` when
    PortAudio reports the configured default is missing, has zero
    channels of the required direction, or can't open at
    ``INPUT_SAMPLE_RATE`` / ``OUTPUT_SAMPLE_RATE``. Caught at
    ``agent.loop.run()`` and used to fall back to ``--text`` mode.
    """


class AudioPlaybackError(AudioError):
    """Output-stream failure detected during ``playback()``.

    Signaled from the PortAudio callback or the no-callback watchdog
    when the output device goes bad mid-stream (unplug, host-API
    stall, consecutive underflows with pending audio). Surfaces at
    the ``await audio.playback(...)`` call site so ``voice_loop``
    can log + print the partial reply as text + continue.
    """


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

# Threshold of consecutive callbacks reporting ``output_underflow`` with
# audio still pending in the buffer before we treat the device as
# failed. ~200 ms at a typical 50-Hz callback rate.
# TODO(#55-tuning): confirm value during manual acceptance test —
# too low: false positives on CPU hiccups / GC pauses; too high:
# user waits too long before the error surfaces.
_PLAYBACK_UNDERFLOW_THRESHOLD = 10

# Secondary watchdog: if the PortAudio callback stops firing entirely
# (device unplug on some host APIs just goes silent instead of
# keeping the underflow flag live), surface ``AudioPlaybackError``
# after this many seconds of callback silence with pending audio.
# Covers the WASAPI unplug case the plan's §3c calls out.
_CALLBACK_SILENCE_S = 2.0


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

    #55 D1 — mid-stream device-failure detection:

    Two cooperating watchdogs route a device failure out of the
    PortAudio callback thread (which cannot raise Python exceptions)
    and into the asyncio task via a shared ``threading.Event`` + a
    ``pipeline_error`` out-param:

    * **Underflow counter.** The callback counts consecutive invocations
      reporting ``status.output_underflow`` while audio is still pending
      in the buffer. Crossing ``_PLAYBACK_UNDERFLOW_THRESHOLD`` (~200 ms
      of callback-reported stall-with-pending-audio) signals the event.
    * **No-callback silence watchdog.** Some PortAudio host APIs stop
      invoking the callback entirely on device unplug (WASAPI's typical
      behavior) instead of keeping the underflow flag live. A separate
      asyncio task polls every 500 ms; if the callback has not fired
      for ``_CALLBACK_SILENCE_S`` seconds while audio is pending, it
      signals the same event.

    When either watchdog fires, ``_error_watcher`` wakes from its
    executor-blocked ``event.wait()``, raises the stashed
    ``AudioPlaybackError``, and that exception propagates out of
    ``playback()`` to the caller. The outer ``finally`` suppresses only
    ``asyncio.CancelledError`` from the task-cleanup awaits so a late
    watcher raise still surfaces (per the plan's §3c PATCH 2).

    Args:
        chunks: async iterator yielding raw PCM bytes (any chunk size).
        device: sounddevice output device index, or ``None`` for default.

    Raises:
        AudioPlaybackError: output device failed mid-stream (persistent
            underflow with pending audio, or callback silence beyond
            ``_CALLBACK_SILENCE_S``).
    """
    queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=PLAYBACK_QUEUE_MAX_CHUNKS)
    buffer = bytearray()
    lock = threading.Lock()
    pipeline_error: list[BaseException | None] = [None]
    error_event = threading.Event()
    underflow_count = [0]
    last_callback_at = [time.monotonic()]

    def _on_output(outdata, frames, time_info, status) -> None:  # PortAudio thread
        # Watchdog heartbeat: single Python-level store, atomic under
        # the GIL. The no-callback watchdog reads this to detect
        # device-silent failures (WASAPI unplug pattern).
        last_callback_at[0] = time.monotonic()
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
            buffer_pending = len(buffer) > 0
        if have < need:
            # Genuine underrun: fill remainder with silence. Producer is
            # either behind or done; the drain loop below exits once the
            # buffer is empty (dangling sub-frame bytes at end-of-stream
            # are discarded as truncated PCM — logging left to #55).
            outdata[have:] = bytes(need - have)
        # #55 D1: persistent underflow WITH pending audio = device not
        # keeping up. Natural end-of-stream underflow has an empty
        # buffer and must not trip this.
        if getattr(status, "output_underflow", False) and buffer_pending:
            underflow_count[0] += 1
            if underflow_count[0] >= _PLAYBACK_UNDERFLOW_THRESHOLD and pipeline_error[0] is None:
                pipeline_error[0] = AudioPlaybackError(
                    f"output stream stalled ({underflow_count[0]} consecutive underflows)"
                )
                error_event.set()
        else:
            underflow_count[0] = 0

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

    async def _error_watcher() -> None:
        # Block on the threading.Event in an executor so the asyncio
        # loop stays free to run the pump + feed. On wake, re-raise
        # the stashed AudioPlaybackError if there is one; a clean
        # wake (set() from the outer finally for shutdown hygiene)
        # returns silently.
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, error_event.wait)
        except asyncio.CancelledError:
            raise
        if pipeline_error[0] is not None:
            raise pipeline_error[0]

    async def _no_callback_watchdog() -> None:
        # Poll every 500 ms. If the callback has not been invoked for
        # _CALLBACK_SILENCE_S seconds AND the buffer has pending
        # audio, treat the device as silent-failed (WASAPI unplug).
        try:
            while True:
                await asyncio.sleep(0.5)
                with lock:
                    pending = len(buffer) > 0
                silent_for = time.monotonic() - last_callback_at[0]
                if pending and silent_for > _CALLBACK_SILENCE_S and pipeline_error[0] is None:
                    pipeline_error[0] = AudioPlaybackError(
                        f"output callback silent for {silent_for:.1f}s "
                        f"with {len(buffer)} bytes pending"
                    )
                    error_event.set()
                    return
        except asyncio.CancelledError:
            raise

    pump_task: asyncio.Task[None] | None = None
    watcher_task: asyncio.Task[None] | None = None
    silence_task: asyncio.Task[None] | None = None
    try:
        stream.start()
        # Reset the heartbeat after start(): the stream warm-up window
        # on Windows (~200-800 ms) can look like silence otherwise.
        last_callback_at[0] = time.monotonic()
        pump_task = asyncio.create_task(_pump(), name="audio-playback-pump")
        watcher_task = asyncio.create_task(_error_watcher(), name="audio-playback-err-watcher")
        silence_task = asyncio.create_task(
            _no_callback_watchdog(), name="audio-playback-silence-watchdog"
        )

        async def _feed() -> None:
            async for chunk in chunks:
                await queue.put(chunk)
            await queue.put(None)  # sentinel; pump exits after processing

        feed_task = asyncio.create_task(_feed(), name="audio-playback-feed")

        # Phase A: feed concurrently with the error watcher. If a
        # watchdog fires during feeding, _error_watcher wins the race
        # and its .result() re-raises AudioPlaybackError.
        done, _pending = await asyncio.wait(
            {feed_task, watcher_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if watcher_task in done:
            watcher_task.result()  # re-raises AudioPlaybackError

        # Phase B (plan §3c PATCH 1): feed done. Wait for the pump to
        # drain OR the watcher to fire. Previous shape awaited the
        # pump unconditionally then entered an unwatched drain loop,
        # which hung on mid-drain device death (callback stops →
        # buffer never empties → silence watchdog fires but main
        # coroutine stuck in asyncio.sleep).
        done2, _pending2 = await asyncio.wait(
            {pump_task, watcher_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if watcher_task in done2:
            watcher_task.result()  # re-raises AudioPlaybackError

        # Phase C: pump completed; drain remaining buffer while STILL
        # watching the watcher — a device dying mid-drain surfaces
        # here before the drain loop can spin forever.
        while True:
            if watcher_task.done():
                watcher_task.result()  # re-raises if device died mid-drain
            with lock:
                if not buffer:
                    break
            await asyncio.sleep(0.02)
    finally:
        # Nested try/finally guarantees stream.stop() + close() run
        # even if a late watcher-task .result() re-raises an
        # AudioPlaybackError during the task-cleanup awaits. Without
        # this nesting, PATCH 2's narrow suppress would let the late
        # raise propagate THROUGH the stream-cleanup lines, leaking a
        # live PortAudio stream.
        try:
            # R3 hygiene: unblock the executor-blocked _error_watcher
            # even on clean completion so no thread-pool worker leaks.
            error_event.set()
            for t in (pump_task, watcher_task, silence_task):
                if t is not None and not t.done():
                    t.cancel()
            # Plan §3c PATCH 2: suppress ONLY CancelledError on the
            # drain awaits. A late AudioPlaybackError from a watchdog
            # that fired after Phase C saw an empty buffer MUST
            # propagate — that's the whole point of the dual-watchdog
            # mechanism. Any non-CancelledError bubbles out, and the
            # outer finally guarantees stream teardown still happens.
            for t in (pump_task, watcher_task, silence_task):
                if t is not None:
                    with contextlib.suppress(asyncio.CancelledError):
                        await t
        finally:
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


def probe_input_device(device: int | None = None) -> None:
    """Verify a usable default input device at the capture format (#55 D5).

    Does NOT open a stream. Queries the configured default (or the
    passed device index), confirms it reports ``INPUT_CHANNELS``+ of
    input channels, and asks PortAudio whether ``INPUT_SAMPLE_RATE`` /
    ``INPUT_DTYPE`` is a supported format on that device via
    ``sd.check_input_settings``.

    Raises:
        AudioDeviceUnavailable: no device matched, or the device has
            zero input channels, or the format isn't supported.
    """
    try:
        if device is None:
            resolved = sd.default.device[0]
        else:
            resolved = device
        info = sd.query_devices(resolved, kind="input")
        if info.get("max_input_channels", 0) < INPUT_CHANNELS:
            raise AudioDeviceUnavailable(
                f"input device {resolved!r} has "
                f"{info.get('max_input_channels', 0)} input channels; "
                f"need at least {INPUT_CHANNELS}"
            )
        sd.check_input_settings(
            device=resolved,
            channels=INPUT_CHANNELS,
            dtype=INPUT_DTYPE,
            samplerate=INPUT_SAMPLE_RATE,
        )
    except sd.PortAudioError as e:
        raise AudioDeviceUnavailable(f"no usable input device: {e}") from e
    except ValueError as e:
        raise AudioDeviceUnavailable(f"input device query failed: {e}") from e


def probe_output_device(device: int | None = None) -> None:
    """Verify a usable default output device at the playback format (#55 D5).

    Symmetric to :func:`probe_input_device` for the output side. See
    that function's docstring for the raise-semantics contract.
    """
    try:
        if device is None:
            resolved = sd.default.device[1]
        else:
            resolved = device
        info = sd.query_devices(resolved, kind="output")
        if info.get("max_output_channels", 0) < OUTPUT_CHANNELS:
            raise AudioDeviceUnavailable(
                f"output device {resolved!r} has "
                f"{info.get('max_output_channels', 0)} output channels; "
                f"need at least {OUTPUT_CHANNELS}"
            )
        sd.check_output_settings(
            device=resolved,
            channels=OUTPUT_CHANNELS,
            dtype=OUTPUT_DTYPE,
            samplerate=OUTPUT_SAMPLE_RATE,
        )
    except sd.PortAudioError as e:
        raise AudioDeviceUnavailable(f"no usable output device: {e}") from e
    except ValueError as e:
        raise AudioDeviceUnavailable(f"output device query failed: {e}") from e


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
