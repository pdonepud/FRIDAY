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
* ``_release`` — wait on the caller's ``ptt_release`` event; when it
  fires, send a ``ForceEndTurn`` control message.
* ``_receive`` — iterate the socket, watching for the terminal
  ``EndOfTurn`` (or a ``FatalError``, connection close, or timeout).

Turn detection is fully suppressed on the Flux side
(``eot_threshold=1.0``) so the application layer owns the turn
boundary. ``eot_timeout_ms=30000`` is the server-side safety net;
``RECEIVE_TIMEOUT_S=35`` is our own outer guard around the receive
loop so a stuck socket surfaces inside the caller's ``await`` rather
than hanging forever.

Audio format is a byte-for-byte match with ``agent.audio``'s capture:
16 kHz mono s16le. No resampling.

See ADR-0003 §STT for design rationale and the migration path to
barge-in in Tier 4.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import AsyncIterator

from deepgram import AsyncDeepgramClient
from deepgram.core.api_error import ApiError
from deepgram.listen.v2.types.listen_v2fatal_error import ListenV2FatalError
from deepgram.listen.v2.types.listen_v2turn_info import ListenV2TurnInfo

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
# Outer safety net around the receive loop: eot_timeout_ms is Flux's
# own backstop; add 5 s of headroom for network round-trip so a stuck
# receive raises inside our async-with rather than hanging the caller.
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

    Server responds ``UNPARSABLE_CLIENT_MESSAGE`` to our control
    message and closes the connection. Enable ForceEndTurn on the
    Deepgram dashboard (Feature Flags) before using this module.
    """


class STTTimeout(STTError):
    """``eot_timeout_ms`` fired before ``ptt_release`` triggered.

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
        # chunks exhausted early is legal — just stop sending; receive
        # keeps listening for the eventual EndOfTurn.
    except asyncio.CancelledError:
        raise
    except STTError as e:
        _fail(done_future, e)
    except Exception as e:
        _fail(done_future, STTError(f"chunk source failed: {e!r}"))


async def _release(socket, ptt_release: asyncio.Event, done_future: asyncio.Future[str]) -> None:
    """Wait for PTT release, then send ForceEndTurn."""
    try:
        await ptt_release.wait()
        await socket.send_force_end_turn()
    except asyncio.CancelledError:
        raise
    except STTError as e:
        _fail(done_future, e)
    except Exception as e:
        # send_force_end_turn raising here often means the deployment
        # doesn't support ForceEndTurn — the receive loop will usually
        # see the connection close first, but if send raises before
        # that, surface it as STTForceEndTurnUnsupported.
        _fail(
            done_future,
            STTForceEndTurnUnsupported(
                f"ForceEndTurn send failed ({e!r}); enable ForceEndTurn "
                f"on the Deepgram dashboard (Feature Flags)."
            ),
        )


async def _receive(socket, done_future: asyncio.Future[str]) -> None:
    """Consume socket messages until a terminal event or timeout."""
    try:
        async with asyncio.timeout(RECEIVE_TIMEOUT_S):
            async for msg in socket:
                if isinstance(msg, ListenV2TurnInfo) and msg.event == "EndOfTurn":
                    _handle_end_of_turn(msg, done_future)
                    return
                if isinstance(msg, ListenV2FatalError):
                    _fail(done_future, STTFatal(f"Flux fatal error: {msg!r}"))
                    return
                # Update, StartOfTurn, EagerEndOfTurn, TurnResumed,
                # Connected, ConfigureSuccess, ConfigureFailure:
                # log-only and keep listening. Configure* would only
                # arrive if we send Configure messages mid-session,
                # which we don't.
                _log.debug("stt receive: non-terminal message %r", msg)
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        _fail(
            done_future,
            STTError(f"no EndOfTurn within {RECEIVE_TIMEOUT_S}s (receive-loop guard)"),
        )
    except STTError as e:
        _fail(done_future, e)
    except Exception as e:
        # ConnectionClosed and other websocket-level errors land here.
        # If we haven't heard back from send_force_end_turn yet, this
        # is likely the deployment rejecting ForceEndTurn.
        _fail(done_future, STTError(f"receive loop failed: {e!r}"))


def _handle_end_of_turn(msg: ListenV2TurnInfo, done_future: asyncio.Future[str]) -> None:
    """Resolve the shared future based on ``msg.trigger``."""
    trigger = msg.trigger
    transcript = msg.transcript
    if trigger == "manual":
        # Happy path: our ForceEndTurn drove the turn end.
        _succeed(done_future, transcript)
    elif trigger == "timeout":
        # Server-side backstop fired. Surface as STTTimeout with
        # the partial transcript so the caller may salvage.
        _fail(done_future, STTTimeout(partial=transcript))
    elif trigger == "model":
        # Flux fired its own EOT despite eot_threshold=1.0. Shouldn't
        # happen; log and return what we got — the caller wanted a
        # transcript and Flux delivered one.
        _log.warning(
            "stt: EndOfTurn with trigger='model' despite eot_threshold=%s; "
            "Flux boundary-condition. Returning transcript anyway.",
            EOT_THRESHOLD,
        )
        _succeed(done_future, transcript)
    else:
        # Open enum per Deepgram docs — new trigger values may appear.
        # Tolerate: log at warning, return transcript.
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
            20 ms chunks (640 bytes) are the intended source.
        ptt_release: asyncio Event that the caller sets when the user
            releases the PTT key. Sending ``ForceEndTurn`` on this
            signal is what ends the turn on the Flux side.

    Returns:
        The finalized transcript from Flux's ``EndOfTurn`` event. May
        be an empty string if the user released without speaking
        (still a valid outcome; error is signalled by ``raise``, not
        by string emptiness).

    Raises:
        STTAuthError: DEEPGRAM_API_KEY is missing or invalid.
        STTTimeout: Flux's ``eot_timeout_ms`` fired before PTT release
            (partial transcript in ``.partial``).
        STTForceEndTurnUnsupported: the Deepgram deployment does not
            have ForceEndTurn enabled.
        STTFatal: Flux emitted a ``FatalError`` mid-session.
        STTError: any other pipeline failure (chunk source raised,
            connection closed, receive-loop guard fired).
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
        # Some SDK versions raise on connect() before the async-with;
        # others raise inside. Handle both.
        raise _wrap_api_error(e) from e

    try:
        async with connect_cm as socket:
            loop = asyncio.get_running_loop()
            done_future: asyncio.Future[str] = loop.create_future()

            pump_task = asyncio.create_task(_pump(socket, chunks, done_future), name="stt-pump")
            release_task = asyncio.create_task(
                _release(socket, ptt_release, done_future), name="stt-release"
            )
            receive_task = asyncio.create_task(_receive(socket, done_future), name="stt-receive")
            tasks = (pump_task, release_task, receive_task)

            try:
                return await done_future
            finally:
                # Cancel all children so the shared drain below is a
                # no-op on already-done tasks.
                for t in tasks:
                    if not t.done():
                        t.cancel()
                # Drain: suppress CancelledError (expected on the tasks
                # we just cancelled), but log any other exception under
                # the task's name — the real outcome is already in
                # done_future, but a straggler is a signal worth surfacing.
                for t in tasks:
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
                    except BaseException as e:
                        _log.warning(
                            "stt: task %s raised during drain: %r",
                            t.get_name(),
                            e,
                        )
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

    Runs one turn: waits for PTT press, records while held, waits for
    PTT release, prints the transcript. Exits cleanly on Ctrl+C.
    """
    # Imports here (not at module top) so ``import agent.stt`` doesn't
    # transitively pull in ``sounddevice`` / ``pynput`` for callers
    # that only want the seam.
    from agent import audio, ptt

    print("[stt] hold Right Alt and speak (Ctrl+C to quit)...", file=sys.stderr)

    ptt_release = asyncio.Event()

    async def _ptt_watcher() -> None:
        """Set the release event on the first PRESSED→RELEASED pair."""
        async for event in ptt.events():
            if event == ptt.PTTEvent.PRESSED:
                print("[stt] pressed", file=sys.stderr)
            elif event == ptt.PTTEvent.RELEASED:
                print("[stt] released", file=sys.stderr)
                ptt_release.set()
                return

    watcher_task = asyncio.create_task(_ptt_watcher(), name="stt-main-watcher")
    try:
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
