"""Deepgram Flux streaming STT with PTT-driven turn endings.

Sole importer of ``deepgram`` in the codebase per ADR-0003 §STT.
Consumers get a single coroutine — ``transcribe`` — plus a typed
exception hierarchy (``STTError`` and subclasses). No leakage of
``deepgram.*`` types across the seam.

Streaming shape: three concurrent asyncio tasks over one Flux
WebSocket, with a shared ``asyncio.Future[str]`` as the single return
point. Each task funnels its outcome (success or typed exception) into
the shared future via ``set_result`` / ``set_exception``. The three
tasks are:

* ``_pump`` — read PCM chunks from the caller's async iterator, send
  each to Flux via ``send_media``.
* ``_release`` — wait on the caller's ``ptt_release`` event; cancel
  and await ``_pump`` (so no ``send_media`` call reaches the socket
  after release), then send a ``ForceEndTurn`` control message and
  set ``force_end_sent``. This arms the receive watchdog.
* ``_receive`` — iterate the raw websocket, watching for the terminal
  ``EndOfTurn`` (or a ``FatalError``, a ``Warning`` announcing the
  turn was silent, connection close, or the post-release watchdog).

Turn detection is fully suppressed on the Flux side
(``eot_threshold=1.0``) so the application layer owns the turn
boundary. ``eot_timeout_ms=30000`` is the server-side safety net;
``RECEIVE_TIMEOUT_S=35`` is our own outer guard, but it only arms
AFTER ``ForceEndTurn`` is sent — an active turn can legitimately run
for as long as the user is speaking.

Audio format is a byte-for-byte match with ``agent.audio``'s capture:
16 kHz mono s16le. No resampling.

See ADR-0003 §STT for design rationale and the migration path to
barge-in in Tier 4.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from collections.abc import AsyncIterator

from deepgram import AsyncDeepgramClient
from deepgram.core.api_error import ApiError
from websockets.exceptions import ConnectionClosed

from agent.audio import INPUT_SAMPLE_RATE

__all__ = [
    "ENCODING",
    "EOT_THRESHOLD",
    "EOT_TIMEOUT_MS",
    "MODEL",
    "RECEIVE_TIMEOUT_S",
    "SAMPLE_RATE",
    "STTAuthError",
    "STTError",
    "STTFatal",
    "STTForceEndTurnUnsupported",
    "STTTimeout",
    "transcribe",
]

_log = logging.getLogger(__name__)

# --- Flux config --------------------------------------------------------
# ADR-0003 §STT locks these values. Do not tune without an ADR update.

MODEL = "flux-general-en"
ENCODING = "linear16"
# Reuse agent.audio's constant so a single edit changes both sides of
# the seam. Currently 16 000 Hz.
SAMPLE_RATE = INPUT_SAMPLE_RATE
# 1.0 fully suppresses Flux's native end-of-turn detection; the
# application drives turn endings via ForceEndTurn per ADR-0003.
EOT_THRESHOLD = 1.0
# Server-side safety net: Flux will force an EndOfTurn after this
# many milliseconds of silence even without a ForceEndTurn message.
# Prevents the server-side turn from hanging if a PTT release event
# is lost or never fires.
EOT_TIMEOUT_MS = 30000
# Outer safety net around the receive loop, armed only AFTER
# ForceEndTurn is sent. Bounds "how long to wait for the terminal
# event"; the active-turn window (before ForceEndTurn) has no such
# bound and can run indefinitely while the user speaks.
RECEIVE_TIMEOUT_S = 35


# --- Exceptions ---------------------------------------------------------


class STTError(Exception):
    """Base class for all errors surfaced by ``transcribe``."""


class STTAuthError(STTError):
    """DEEPGRAM_API_KEY is missing, invalid, or lacks Flux access.

    Wraps Deepgram's ``ApiError`` when it comes back with 401.
    """


class STTFatal(STTError):
    """Flux emitted a ``FatalError`` message during the session."""


class STTForceEndTurnUnsupported(STTError):
    """The Deepgram deployment does not have ForceEndTurn enabled.

    Server responds with a ``FatalError`` whose ``code`` field is
    ``UNPARSABLE_CLIENT_MESSAGE`` and closes the socket. Enable
    ForceEndTurn on the Deepgram dashboard (Feature Flags) before
    using this module.
    """


class STTTimeout(STTError):
    """``eot_timeout_ms`` fired before the caller drove the turn to end.

    Carries the partial transcript so the caller can salvage it if
    desired. Distinct from ``STTError`` so a caller can distinguish
    "safety-net fired" from "user drove the turn to completion" via
    ``except STTTimeout as e: use e.partial``.
    """

    def __init__(self, partial: str) -> None:
        super().__init__(f"Flux eot_timeout_ms fired; partial transcript: {partial!r}")
        self.partial = partial


# --- Lazy client (mirrors agent.claude._get_client) ---------------------

_client: AsyncDeepgramClient | None = None


def _get_client() -> AsyncDeepgramClient:
    """Return the shared Deepgram client, constructing it on first use.

    Lazy so importing this module has no side effects and does not
    require ``DEEPGRAM_API_KEY`` at import time. Callers must have
    loaded the environment (e.g. via ``python-dotenv``) before the
    first invocation. Mirrors ``agent.claude._get_client``.
    """
    global _client
    if _client is None:
        _client = AsyncDeepgramClient()
    return _client


# --- Task coroutines ----------------------------------------------------
# Each of the three tasks funnels its outcome into ``done_future`` via
# ``set_result`` / ``set_exception``. The ``if not done_future.done():``
# guard is deliberate — the first task to resolve the future wins; the
# rest are cancelled by transcribe()'s finally.


def _fail(done_future: asyncio.Future[str], exc: BaseException) -> None:
    """Set an exception on the shared future if it hasn't resolved yet."""
    if not done_future.done():
        done_future.set_exception(exc)


def _succeed(done_future: asyncio.Future[str], value: str) -> None:
    """Set a result on the shared future if it hasn't resolved yet."""
    if not done_future.done():
        done_future.set_result(value)


async def _pump(socket, chunks: AsyncIterator[bytes], done_future: asyncio.Future[str]) -> None:
    """Send PCM chunks to Flux until the source exhausts or we're cancelled."""
    try:
        async for chunk in chunks:
            await socket.send_media(chunk)
        # Chunks exhausted early is legal — just stop sending; receive
        # keeps listening for the eventual EndOfTurn.
    except asyncio.CancelledError:
        raise
    except STTError as e:
        _fail(done_future, e)
    except Exception as e:
        _fail(done_future, STTError(f"chunk source failed: {e!r}"))


async def _release(
    socket,
    ptt_release: asyncio.Event,
    pump_task: asyncio.Task[None],
    force_end_sent: asyncio.Event,
    done_future: asyncio.Future[str],
) -> None:
    """Wait for PTT release, quiesce ``_pump``, then send ForceEndTurn.

    Invariant preservation: if ``_pump`` failed before ``cancel()``
    took effect, its exception was already recorded on ``done_future``
    by ``_pump``'s own error path — ``await pump_task`` re-raises
    only ``CancelledError`` in that case (the swallowed exception is
    NOT re-raised through the task). This function must NOT relabel
    any pump-side error as "ForceEndTurn send failed"; we only wrap
    the send itself.
    """
    try:
        await ptt_release.wait()
    except asyncio.CancelledError:
        raise

    # Cancel ``_pump`` BEFORE sending ForceEndTurn (Finding 1). No
    # send_media() call may reach the socket after the release signal.
    pump_task.cancel()
    try:
        await pump_task
    except asyncio.CancelledError:
        pass
    # Any other pump exception is impossible here because _pump swallows
    # them into done_future via its own except-clause and returns
    # normally. If a future edit removes that swallow, the exception
    # would propagate here — that is desirable; do not add a
    # blanket except that would relabel it.

    try:
        await socket.send_force_end_turn()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # Finding 3: send-side exceptions are transport failures, not
        # necessarily rejection. The specific STTForceEndTurnUnsupported
        # mapping lives in _receive on the server-sent FatalError with
        # code=UNPARSABLE_CLIENT_MESSAGE.
        _fail(done_future, STTError(f"ForceEndTurn send failed: {e!r}"))
        return
    force_end_sent.set()


async def _receive(socket, force_end_sent: asyncio.Event, done_future: asyncio.Future[str]) -> None:
    """Consume raw websocket JSON until a terminal event, close, or watchdog.

    Iterates ``socket._websocket`` (a private-but-stable Fern-generated
    attribute) rather than the SDK's public ``__aiter__``. Reason: the
    public iterator uses ``construct_type`` on a typed union that does
    NOT include ``Warning`` messages, so unknown-type payloads are
    silently yielded as ``None`` with no way to recover the raw JSON.
    We need the JSON to see ``FORCE_END_TURN_NO_ACTIVE_TURN`` warnings
    (Finding 2). Tracked upstream at
    https://github.com/deepgram/deepgram-python-sdk/issues/792 — if
    resolved upstream, migrate back to the typed iterator and delete
    this workaround. The ``deepgram-sdk==7.8.1`` pin in
    ``agent/requirements.txt`` bounds the coupling.
    """
    if not hasattr(socket, "_websocket"):
        _fail(
            done_future,
            STTError(
                "SDK internal 'socket._websocket' missing — "
                "pin mismatch, check deepgram-sdk version"
            ),
        )
        return

    ws = socket._websocket

    async def _watchdog() -> None:
        """Sleep until ``force_end_sent`` fires, then bound the wait for EndOfTurn."""
        await force_end_sent.wait()
        await asyncio.sleep(RECEIVE_TIMEOUT_S)
        _fail(
            done_future,
            STTError(f"no EndOfTurn within {RECEIVE_TIMEOUT_S}s of ForceEndTurn"),
        )

    watchdog_task = asyncio.create_task(_watchdog(), name="stt-receive-watchdog")
    try:
        async for raw in ws:
            if isinstance(raw, bytes):
                # Server-originated binary frames are not part of Flux's
                # documented protocol; ignore rather than crash.
                continue
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "TurnInfo" and msg.get("event") == "EndOfTurn":
                _handle_end_of_turn(msg, done_future)
                return

            if msg_type == "Warning":
                code = msg.get("code")
                if code == "FORCE_END_TURN_NO_ACTIVE_TURN":
                    # Finding 2: ForceEndTurn arrived before StartOfTurn.
                    # Silent PTT turn — no active turn to end, no
                    # transcript to return. Treat as clean empty success.
                    _succeed(done_future, "")
                    return
                _log.warning(
                    "stt: Warning code=%r description=%r",
                    code,
                    msg.get("description"),
                )
                continue

            if msg_type == "Error":
                code = msg.get("code")
                if code == "UNPARSABLE_CLIENT_MESSAGE":
                    # Finding 3: server rejected our ForceEndTurn.
                    _fail(
                        done_future,
                        STTForceEndTurnUnsupported(
                            "ForceEndTurn not enabled on this Deepgram deployment "
                            "(FatalError code=UNPARSABLE_CLIENT_MESSAGE). "
                            "Enable ForceEndTurn on the Deepgram dashboard "
                            "(Feature Flags)."
                        ),
                    )
                else:
                    _fail(
                        done_future,
                        STTFatal(f"Flux fatal (code={code!r}): {msg.get('description')!r}"),
                    )
                return

            # Update, StartOfTurn, EagerEndOfTurn, TurnResumed, Connected,
            # ConfigureSuccess, ConfigureFailure: log-only, keep listening.
            _log.debug("stt receive: non-terminal type=%r", msg_type)

        # Finding 5: iterator exhausted without a terminal event.
        _fail(done_future, STTError("socket closed without EndOfTurn"))
    except asyncio.CancelledError:
        raise
    except ConnectionClosed as e:
        _fail(done_future, STTError(f"connection closed: {e!r}"))
    except Exception as e:
        _fail(done_future, STTError(f"receive loop failed: {e!r}"))
    finally:
        if not watchdog_task.done():
            watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog_task


def _handle_end_of_turn(msg: dict, done_future: asyncio.Future[str]) -> None:
    """Resolve the shared future based on ``msg["trigger"]``."""
    trigger = msg.get("trigger")
    transcript = msg.get("transcript", "")
    if trigger == "manual":
        _succeed(done_future, transcript)
    elif trigger == "timeout":
        _fail(done_future, STTTimeout(partial=transcript))
    elif trigger == "model":
        _log.warning(
            "stt: EndOfTurn with trigger='model' despite eot_threshold=%s; "
            "Flux boundary-condition. Returning transcript anyway.",
            EOT_THRESHOLD,
        )
        _succeed(done_future, transcript)
    else:
        _log.warning(
            "stt: EndOfTurn with unknown trigger=%r; returning transcript.",
            trigger,
        )
        _succeed(done_future, transcript)


# --- Public API ---------------------------------------------------------


async def transcribe(chunks: AsyncIterator[bytes], ptt_release: asyncio.Event) -> str:
    """Stream PCM chunks to Deepgram Flux; return the transcript on PTT release.

    Args:
        chunks: async iterator yielding 16 kHz mono s16le PCM bytes.
            Chunk size is not constrained; ``agent.audio.capture()``'s
            20 ms chunks (640 bytes) are the intended source. Callers
            SHOULD NOT start yielding chunks until the physical PTT
            press has occurred — see ``_main`` for the reference
            pattern.
        ptt_release: asyncio Event that the caller sets when the user
            releases the PTT key. This drives ``ForceEndTurn`` and
            arms the receive-side watchdog.

    Returns:
        The finalized transcript from Flux's ``EndOfTurn`` event, or
        ``""`` if the PTT turn contained no speech (Flux returned
        ``Warning FORCE_END_TURN_NO_ACTIVE_TURN``). Empty is a valid
        outcome; error is signalled by ``raise``, not by string
        emptiness.

    Raises:
        STTAuthError: DEEPGRAM_API_KEY is missing or invalid.
        STTTimeout: Flux's ``eot_timeout_ms`` fired before the client
            drove the turn to completion (partial transcript in
            ``.partial``).
        STTForceEndTurnUnsupported: the Deepgram deployment does not
            have ForceEndTurn enabled (FatalError code
            ``UNPARSABLE_CLIENT_MESSAGE``).
        STTFatal: Flux emitted a ``FatalError`` with any other code.
        STTError: any other pipeline failure (chunk source raised,
            connection closed, receive-side watchdog fired after
            ForceEndTurn, socket closed without a terminal event,
            ForceEndTurn send failed for transport reasons).
    """
    try:
        client = _get_client()
    except Exception as e:
        raise STTError(f"failed to construct Deepgram client: {e!r}") from e

    try:
        connect_cm = client.listen.v2.connect(
            model=MODEL,
            encoding=ENCODING,
            sample_rate=SAMPLE_RATE,
            eot_threshold=EOT_THRESHOLD,
            eot_timeout_ms=EOT_TIMEOUT_MS,
        )
    except ApiError as e:
        raise _wrap_api_error(e) from e

    try:
        async with connect_cm as socket:
            loop = asyncio.get_running_loop()
            done_future: asyncio.Future[str] = loop.create_future()
            force_end_sent = asyncio.Event()

            pump_task = asyncio.create_task(_pump(socket, chunks, done_future), name="stt-pump")
            release_task = asyncio.create_task(
                _release(socket, ptt_release, pump_task, force_end_sent, done_future),
                name="stt-release",
            )
            receive_task = asyncio.create_task(
                _receive(socket, force_end_sent, done_future), name="stt-receive"
            )
            tasks = (pump_task, release_task, receive_task)

            try:
                return await done_future
            finally:
                for t in tasks:
                    if not t.done():
                        t.cancel()
                for t in tasks:
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
                    except BaseException as e:
                        _log.warning("stt: task %s raised during drain: %r", t.get_name(), e)
    except ApiError as e:
        raise _wrap_api_error(e) from e


def _wrap_api_error(exc: ApiError) -> STTError:
    """Translate a Deepgram ``ApiError`` into our exception hierarchy."""
    status = getattr(exc, "status_code", None)
    if status == 401:
        return STTAuthError(
            "DEEPGRAM_API_KEY missing or invalid (401 from Deepgram); see .env.example."
        )
    return STTError(f"Deepgram API error (status={status}): {exc!r}")


# --- __main__ helpers ---------------------------------------------------
# The ``if __name__ == "__main__":`` dispatch below carries a
# ``# pragma: no cover`` per the precedent set in #48. The ``_main``
# coroutine itself is unit-tested with monkey-patched deps.


async def _main() -> None:
    """Wire audio.capture + ptt.events + transcribe, print the transcript.

    _main is single-turn by design; multi-turn loop belongs to #53.
    Waits for the first PRESSED before consuming any audio (Finding 6)
    so nothing captured before the physical press can reach Deepgram.
    """
    # Imports here (not at module top) so ``import agent.stt`` doesn't
    # transitively pull in ``sounddevice`` / ``pynput`` for callers
    # that only want the seam.
    from agent import audio, ptt

    print("[stt] hold Right Alt and speak (Ctrl+C to quit)...", file=sys.stderr)

    ptt_release = asyncio.Event()
    press_seen = asyncio.Event()

    async def _ptt_watcher() -> None:
        async for event in ptt.events():
            if event == ptt.PTTEvent.PRESSED:
                print("[stt] pressed", file=sys.stderr)
                press_seen.set()
            elif event == ptt.PTTEvent.RELEASED:
                print("[stt] released", file=sys.stderr)
                ptt_release.set()
                return

    watcher_task = asyncio.create_task(_ptt_watcher(), name="stt-main-watcher")
    try:
        # Finding 6: do not start audio.capture() until PRESSED. No
        # send_media call can reach Deepgram before the physical press.
        await press_seen.wait()
        transcript = await transcribe(audio.capture(), ptt_release)
        print(f"[stt] transcript: {transcript!r}")
    finally:
        if not watcher_task.done():
            watcher_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher_task


if __name__ == "__main__":  # pragma: no cover
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        sys.exit(0)
