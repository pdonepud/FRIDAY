"""Deepgram Flux streaming STT with PTT-driven turn endings.

Sole importer of ``deepgram`` in the codebase per ADR-0003 §STT.
Consumers get two coroutines — ``transcribe`` (PTT-scoped, used
during LISTENING to produce the user's transcript) and
``detect_barge_in`` (SPEAKING-scoped, fires an ``asyncio.Event`` on
a confirmed Flux ``StartOfTurn`` for the barge-in path in #72) —
plus a typed exception hierarchy (``STTError`` and subclasses). No
leakage of ``deepgram.*`` types across the seam.

Streaming shape: three concurrent asyncio tasks over one Flux
WebSocket, with a shared ``asyncio.Future[str]`` as the single return
point. Each task funnels its outcome (success or typed exception) into
the shared future via ``set_result`` / ``set_exception``. The three
tasks are:

* ``_pump`` — read PCM chunks from the caller's async iterator, send
  each to Flux via ``send_media``.
* ``_release`` — wait on the caller's ``ptt_release`` event; cancel
  and await ``_pump`` (so no ``send_media`` call reaches the socket
  after release); then wait up to ``PTT_RELEASE_GRACE_MS`` for a
  natural ``EndOfTurn`` signalled by ``_receive``. If the signal
  fires in grace, no ``ForceEndTurn`` is sent. If grace expires,
  ``ForceEndTurn`` fires as a bounded liveness fallback and
  ``force_end_sent`` is set, arming the receive watchdog.
* ``_receive`` — iterate the raw websocket, watching for the terminal
  ``EndOfTurn`` (or a ``FatalError``, a ``Warning`` announcing the
  turn was silent, connection close, or the post-release watchdog).
  Also tracks the speculative-finalization flag driven by
  ``EagerEndOfTurn`` / ``TurnResumed`` so pending-eager retractions
  are a no-op on ``done_future``.

Flux is configured with ``eot_threshold=EOT_THRESHOLD`` and
``eager_eot_threshold=EAGER_EOT_THRESHOLD`` (see ADR-0005 Decision 1).
``eot_timeout_ms=30000`` is the server-side safety net;
``RECEIVE_TIMEOUT_S=35`` is our own outer guard, but it only arms
AFTER ``ForceEndTurn`` is sent — an active turn can legitimately run
for as long as the user is speaking.

Audio format is a byte-for-byte match with ``agent.audio``'s capture:
16 kHz mono s16le. No resampling.

See ADR-0003 §STT and ADR-0005 for design rationale. The
StartOfTurn-during-SPEAKING barge-in detection ADR-0005 Decision 1
describes is #72's work; this module handles the Flux-config and
event-recognition prerequisites.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from collections.abc import AsyncIterator

from deepgram import AsyncDeepgramClient
from deepgram.core.api_error import ApiError
from websockets.exceptions import ConnectionClosed, WebSocketException

from agent.audio import INPUT_SAMPLE_RATE

__all__ = [
    "BARGEIN_CONFIRM_MS",
    "EAGER_EOT_THRESHOLD",
    "ENCODING",
    "EOT_THRESHOLD",
    "EOT_TIMEOUT_MS",
    "MODEL",
    "PTT_RELEASE_GRACE_MS",
    "RECEIVE_TIMEOUT_S",
    "SAMPLE_RATE",
    "STTAuthError",
    "STTError",
    "STTFatal",
    "STTForceEndTurnUnsupported",
    "STTTimeout",
    "detect_barge_in",
    "transcribe",
]

_log = logging.getLogger(__name__)

# --- Flux config --------------------------------------------------------
# Values are the Tier-4a baseline per ADR-0005 Decision 1; the
# TODO(#70-tuning) markers below call out the ones expected to move
# as real-world use surfaces data.

MODEL = "flux-general-en"
ENCODING = "linear16"
# Reuse agent.audio's constant so a single edit changes both sides of
# the seam. Currently 16 000 Hz.
SAMPLE_RATE = INPUT_SAMPLE_RATE
# Lowered from 1.0 (Tier 3's "Flux-side turn detection off" posture, per
# ADR-0003 §STT) back toward Flux's ~0.7 default so natural EndOfTurn fires
# on conversational silences. ForceEndTurn is retained as a bounded
# liveness fallback via PTT_RELEASE_GRACE_MS.
# TODO(#70-tuning): revisit after real-world use.
EOT_THRESHOLD = 0.7
# Speculative turn finalization during active PTT. Must satisfy
# EAGER_EOT_THRESHOLD <= EOT_THRESHOLD (not SDK-enforced in
# deepgram-sdk==7.8.1 — asserted below).
# TODO(#70-tuning): revisit after real-world use.
EAGER_EOT_THRESHOLD = 0.5
# Server-side safety net: Flux will force an EndOfTurn after this
# many milliseconds of silence even without a ForceEndTurn message.
# Prevents the server-side turn from hanging if a PTT release event
# is lost or never fires.
EOT_TIMEOUT_MS = 30000
# Bounded wait after PTT RELEASE for Flux to emit EndOfTurn naturally
# (via EagerEndOfTurn confirmation or on sustained silence) before
# ForceEndTurn fires as a liveness fallback. Per ADR-0005 Decision 1.
# TODO(#70-tuning): revisit after real-world use.
PTT_RELEASE_GRACE_MS = 500
# Confirmation window after a Flux StartOfTurn observed during SPEAKING
# (via ``detect_barge_in``). 150 ms ≈ 3 frames at a 50 ms Flux chunk
# cadence; filters single-event flaps (throat clears, Lily's own
# voice bleeding through) by requiring a second speech-indicative
# event — Update with transcript, EagerEndOfTurn, EndOfTurn with
# transcript, or TurnResumed — before firing the barge-in signal.
# Per ADR-0005 Decision 2.
# TODO(#72-tuning): revisit after real-world use (headphones vs.
# speakers, room noise, Lily's voice latency into the open mic).
BARGEIN_CONFIRM_MS = 150
# Outer safety net around the receive loop, armed only AFTER
# ForceEndTurn is sent. Bounds "how long to wait for the terminal
# event"; the active-turn window (before ForceEndTurn) has no such
# bound and can run indefinitely while the user speaks.
RECEIVE_TIMEOUT_S = 35

# Client-side invariant: Flux requires eager <= eot but the current
# deepgram-sdk==7.8.1 ships ListenV2ConfigureThresholds with
# Optional[Any] fields and no validator. This bare assert is the
# only client-side guard; it fires at import time so a bad commit
# can't ship behind a lazy code path.
assert EAGER_EOT_THRESHOLD <= EOT_THRESHOLD, (
    f"Flux config invariant violated: EAGER_EOT_THRESHOLD={EAGER_EOT_THRESHOLD} "
    f"must be <= EOT_THRESHOLD={EOT_THRESHOLD}"
)


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
    """Send PCM chunks to Flux until the source exhausts or we're cancelled.

    #55 D7: the outer try/except catches failures from iterating
    ``chunks`` (the caller's audio source — typically
    ``agent.audio.capture`` under voice_loop). An inner try/except
    around the ``send_media`` call distinguishes socket-send
    failures so error wording accurately reflects the actual seam
    that broke, rather than labelling every mid-turn error as
    "chunk source failed". Both paths route through ``_fail`` so
    the shared future wins the race against ``_receive``/``_release``.
    """
    try:
        async for chunk in chunks:
            try:
                await socket.send_media(chunk)
            except asyncio.CancelledError:
                raise
            except (OSError, TimeoutError, WebSocketException) as e:
                # Transport-side failure during a WebSocket send — mirrors
                # the same tuple ``transcribe`` catches at the handshake
                # site (see line 456). Specific wording so a reviewer
                # chasing a mid-turn error knows the send side died, not
                # the mic source.
                _fail(done_future, STTError(f"audio send failed: {e!r}"))
                return
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
    eot_received: asyncio.Event,
    done_future: asyncio.Future[str],
) -> None:
    """Wait for PTT release, quiesce ``_pump``, then either observe a natural
    ``EndOfTurn`` within ``PTT_RELEASE_GRACE_MS`` or send ``ForceEndTurn``.

    ADR-0005 Decision 1 (bounded release grace): with
    ``EOT_THRESHOLD=0.7`` Flux can finalize naturally on
    conversational silence after release, so a smooth turn end gets
    the SPEAKING state transition without a server round-trip. The
    grace is strictly bounded — if ``eot_received`` has not fired
    within ``PTT_RELEASE_GRACE_MS``, ``ForceEndTurn`` is sent as the
    liveness fallback (Tier-3 posture preserved).

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

    # Cancel ``_pump`` BEFORE anything else (Finding 1). No
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

    # Bounded wait for a natural EndOfTurn from _receive. If one
    # arrives, ForceEndTurn is skipped entirely — _receive has
    # already resolved done_future with the transcript. If the grace
    # expires, fall through to the ForceEndTurn liveness fallback.
    loop = asyncio.get_running_loop()
    start_t = loop.time()
    try:
        await asyncio.wait_for(
            eot_received.wait(),
            timeout=PTT_RELEASE_GRACE_MS / 1000,
        )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        elapsed_ms = int((loop.time() - start_t) * 1000)
        _log.warning(
            "stt: PTT release grace expired after %dms, sending ForceEndTurn liveness fallback",
            elapsed_ms,
        )
    else:
        elapsed_ms = int((loop.time() - start_t) * 1000)
        _log.debug(
            "stt: natural EndOfTurn arrived %dms after PTT release, skipping ForceEndTurn",
            elapsed_ms,
        )
        return

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


async def _receive(
    socket,
    ptt_release: asyncio.Event,
    force_end_sent: asyncio.Event,
    eot_received: asyncio.Event,
    done_future: asyncio.Future[str],
) -> None:
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

    ADR-0005 Decision 1 event handling:

    * ``EagerEndOfTurn`` — speculative finalization. Sets a local
      ``pending_eager`` flag; done_future is NOT resolved until a
      confirming ``EndOfTurn`` arrives.
    * ``TurnResumed`` — retracts a pending speculative finalization.
      If no ``EagerEndOfTurn`` was pending, a WARNING is logged with
      the turn_index and the event is otherwise ignored (resilience
      over strictness).
    * ``EndOfTurn`` with ``trigger="manual"`` — terminal (response to
      our ``ForceEndTurn``). Sets ``eot_received`` and resolves
      ``done_future``.
    * ``EndOfTurn`` with ``trigger="model"`` — Flux native detection.
      Terminal ONLY if ``ptt_release`` is already set (the user has
      released and we're inside the release grace). Otherwise ignored
      with a DEBUG log — the user is mid-utterance and we keep
      listening until they stop. This is the ADR-0005 Decision 1
      posture: ``EOT_THRESHOLD=0.7`` lets Flux end turns naturally
      POST-release, not DURING the user's active hold.

    Transcripts from ignored model-triggered EndOfTurns are
    accumulated locally and prepended (space-joined) to the
    terminal EndOfTurn's transcript before done_future resolves.
    Flux emits one EndOfTurn per detected turn and starts fresh
    after each; without accumulation, every mid-utterance silence
    Flux decides is a turn boundary would silently truncate the
    returned transcript.
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

    pending_eager = False
    # Transcripts from EndOfTurns we ignore because they fired on a
    # mid-utterance silence during active PTT. Flux starts a fresh
    # turn after each EndOfTurn, so without this accumulator the
    # terminal EndOfTurn would only carry speech from after the last
    # ignored silence and everything earlier would be lost.
    accumulated_transcripts: list[str] = []
    watchdog_task = asyncio.create_task(_watchdog(), name="stt-receive-watchdog")
    try:
        async for raw in ws:
            if isinstance(raw, bytes):
                # Server-originated binary frames are not part of Flux's
                # documented protocol; ignore rather than crash.
                continue
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "TurnInfo":
                event = msg.get("event")
                if event == "EndOfTurn":
                    trigger = msg.get("trigger")
                    if trigger == "model" and not ptt_release.is_set():
                        # Flux native EndOfTurn fired on a mid-utterance
                        # silence while the user is still holding PTT.
                        # Keep the transcript Flux emitted so it survives
                        # the next turn boundary, then ignore the event
                        # and keep listening. When PTT releases,
                        # _release()'s grace will wait for the next
                        # natural EndOfTurn (or send ForceEndTurn as the
                        # bounded liveness fallback). ADR-0005
                        # Decision 1.
                        ignored_transcript = msg.get("transcript", "") or ""
                        if ignored_transcript:
                            accumulated_transcripts.append(ignored_transcript)
                        _log.debug(
                            "stt: ignoring model-triggered EndOfTurn "
                            "(turn_index=%r, transcript_len=%d) during active PTT; "
                            "user still speaking",
                            msg.get("turn_index"),
                            len(ignored_transcript),
                        )
                        pending_eager = False
                        continue
                    # Terminal path: trigger="manual", trigger="model"
                    # post-release, trigger="timeout", or unknown. Join
                    # any transcripts accumulated from mid-hold ignored
                    # EndOfTurns with this terminal transcript so the
                    # full utterance survives Flux's per-turn transcript
                    # model. Mutate a shallow copy so the original msg
                    # isn't altered for anything inspecting it later.
                    if accumulated_transcripts:
                        final_transcript = msg.get("transcript", "") or ""
                        joined = " ".join(accumulated_transcripts + [final_transcript]).strip()
                        msg = dict(msg)
                        msg["transcript"] = joined
                        _log.debug(
                            "stt: joined %d ignored model EndOfTurn transcript(s) "
                            "with terminal transcript; final length=%d",
                            len(accumulated_transcripts),
                            len(joined),
                        )
                    pending_eager = False
                    # Signal _release BEFORE resolving done_future so
                    # a release-task waiting on grace sees eot_received
                    # in-grace and skips ForceEndTurn even if the
                    # done_future resolution unblocks transcribe()
                    # first.
                    eot_received.set()
                    _handle_end_of_turn(msg, done_future)
                    return
                if event == "EagerEndOfTurn":
                    _log.debug(
                        "stt: EagerEndOfTurn (turn_index=%r); speculative, pending confirmation",
                        msg.get("turn_index"),
                    )
                    pending_eager = True
                    continue
                if event == "TurnResumed":
                    if pending_eager:
                        _log.debug(
                            "stt: TurnResumed (turn_index=%r); retracted pending EagerEndOfTurn",
                            msg.get("turn_index"),
                        )
                        pending_eager = False
                    else:
                        _log.warning(
                            "stt: TurnResumed (turn_index=%r) with no pending EagerEndOfTurn; "
                            "ignoring",
                            msg.get("turn_index"),
                        )
                    continue
                # Update, StartOfTurn: log and keep listening.
                _log.debug("stt receive: non-terminal TurnInfo event=%r", event)
                continue

            if msg_type == "Warning":
                code = msg.get("code")
                if code == "FORCE_END_TURN_NO_ACTIVE_TURN":
                    # Finding 2: ForceEndTurn arrived before StartOfTurn.
                    # Silent PTT turn — no active turn to end. Set
                    # eot_received so a release still in grace exits
                    # cleanly (it won't be — this warning is a response
                    # to a ForceEndTurn that already went out — but
                    # keep the signal consistent).
                    #
                    # Also returns any transcripts accumulated from
                    # mid-hold ignored model-triggered EndOfTurns:
                    # those closed Flux turns with real speech, so the
                    # fact that no active turn remained when
                    # ForceEndTurn arrived (user fell silent after
                    # Flux's last natural EOT) must not drop them.
                    # Truly silent PTT (empty accumulator) still
                    # returns "".
                    eot_received.set()
                    final = (
                        " ".join(accumulated_transcripts).strip() if accumulated_transcripts else ""
                    )
                    _succeed(done_future, final)
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

            # Connected, ConfigureSuccess, ConfigureFailure: log-only.
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
        _log.debug("stt: natural EndOfTurn (trigger='model')")
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
        STTAuthError: DEEPGRAM_API_KEY is missing, unset, empty, or invalid.
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
    except ApiError as e:
        # SDK 7.8.1 raises ApiError from AsyncBaseClient.__init__ when
        # api_key is None (DEEPGRAM_API_KEY unset). Route through
        # _wrap_api_error so the status_code=None+empty-env-var case
        # maps to STTAuthError rather than generic STTError.
        raise _wrap_api_error(e) from e
    except Exception as e:
        raise STTError(f"failed to construct Deepgram client: {e!r}") from e

    try:
        connect_cm = client.listen.v2.connect(
            model=MODEL,
            encoding=ENCODING,
            sample_rate=SAMPLE_RATE,
            eot_threshold=EOT_THRESHOLD,
            eager_eot_threshold=EAGER_EOT_THRESHOLD,
            eot_timeout_ms=EOT_TIMEOUT_MS,
        )
    except ApiError as e:
        raise _wrap_api_error(e) from e

    try:
        async with connect_cm as socket:
            loop = asyncio.get_running_loop()
            done_future: asyncio.Future[str] = loop.create_future()
            force_end_sent = asyncio.Event()
            # eot_received bridges _receive -> _release so a natural
            # EndOfTurn within PTT_RELEASE_GRACE_MS can skip
            # ForceEndTurn entirely (ADR-0005 Decision 1).
            eot_received = asyncio.Event()

            pump_task = asyncio.create_task(_pump(socket, chunks, done_future), name="stt-pump")
            release_task = asyncio.create_task(
                _release(
                    socket,
                    ptt_release,
                    pump_task,
                    force_end_sent,
                    eot_received,
                    done_future,
                ),
                name="stt-release",
            )
            receive_task = asyncio.create_task(
                _receive(socket, ptt_release, force_end_sent, eot_received, done_future),
                name="stt-receive",
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
    except (OSError, TimeoutError, WebSocketException) as e:
        # SDK 7.8.1's AsyncV2Client.connect (deepgram/listen/v2/client.py:306-321)
        # only translates InvalidWebSocketStatus to ApiError; DNS, TCP, TLS,
        # timeout, and other websockets-level failures escape as-is. Wrap
        # them to keep the STTError contract intact.
        raise STTError(f"connection failed: {e!r}") from e


def _wrap_api_error(exc: ApiError) -> STTError:
    """Translate a Deepgram ``ApiError`` into our exception hierarchy."""
    status = getattr(exc, "status_code", None)
    if status == 401:
        return STTAuthError(
            "DEEPGRAM_API_KEY missing or invalid (401 from Deepgram); see .env.example."
        )
    if status is None and not os.environ.get("DEEPGRAM_API_KEY"):
        # SDK 7.8.1's AsyncBaseClient.__init__ raises ApiError with
        # status_code=None when api_key is None — i.e. DEEPGRAM_API_KEY
        # is unset or empty. See deepgram/base_client.py:266-269.
        # Distinct from a 401 which fires later during connect(). Any
        # other status_code=None case (env var set, unknown ApiError
        # shape) falls through to the generic bucket below —
        # evidence-based routing over shape-based routing.
        return STTAuthError("DEEPGRAM_API_KEY missing (unset or empty); see .env.example.")
    return STTError(f"Deepgram API error (status={status}): {exc!r}")


# --- Barge-in detector (#72 / ADR-0005 Decision 2) ---------------------
# Separate entry point from ``transcribe`` so each primitive stays
# single-responsibility. ``transcribe`` is PTT-scoped (returns the
# user's transcript on EndOfTurn); ``detect_barge_in`` is
# SPEAKING-scoped (fires a shared ``asyncio.Event`` on a confirmed
# StartOfTurn and keeps running until cancelled). ADR-0005
# Alternatives explicitly rejected multiplexing the two modes behind
# one call.


async def _mic_pump(socket, mic_chunks: AsyncIterator[bytes]) -> None:
    """Drain ``mic_chunks`` into the Flux session for ``detect_barge_in``.

    Simpler than ``_pump``: no shared future, so transport failures
    propagate as ``STTError`` to the caller instead of being
    funneled through ``done_future``. ``CancelledError`` is the
    normal exit path — ``detect_barge_in``'s ``finally`` cancels
    this task once a barge-in has been confirmed or the caller has
    cancelled.
    """
    try:
        async for chunk in mic_chunks:
            await socket.send_media(chunk)
    except asyncio.CancelledError:
        raise
    except (OSError, TimeoutError, WebSocketException) as e:
        raise STTError(f"audio send failed: {e!r}") from e
    except STTError:
        raise
    except Exception as e:
        raise STTError(f"mic source failed: {e!r}") from e


async def _watch_for_barge_in(socket, barge_in_event: asyncio.Event, confirmation_ms: int) -> None:
    """Watch Flux events for a confirmed ``StartOfTurn``.

    Protocol:

    * On ``StartOfTurn``: (re)arm the confirmation window as a
      ``confirm_timer`` task that sleeps ``confirmation_ms`` ms.
      A later ``StartOfTurn`` within or after the window re-arms.
    * On a speech-indicative follow-up event (``Update`` with
      non-empty transcript, ``EagerEndOfTurn``, ``EndOfTurn`` with
      transcript, or ``TurnResumed``) **while** ``confirm_timer``
      is still running: fire ``barge_in_event``, cancel the timer,
      return.
    * On the same follow-up events after ``confirm_timer`` has
      completed (window expired): discard, keep listening for the
      next ``StartOfTurn``.
    * On a ``Warning`` with code ``FORCE_END_TURN_NO_ACTIVE_TURN``:
      Flux saw no active turn — the StartOfTurn was a flap. Discard
      any pending confirmation and keep listening.
    * On ``Error`` events: translate to ``STTForceEndTurnUnsupported``
      / ``STTFatal`` the same way ``_receive`` does.

    The caller cancels this coroutine when leaving SPEAKING. The
    final ``finally`` always cancels the pending ``confirm_timer``
    so no stale timer leaks past the detector's lifetime.
    """
    if not hasattr(socket, "_websocket"):
        raise STTError(
            "SDK internal 'socket._websocket' missing — pin mismatch, check deepgram-sdk version"
        )

    ws = socket._websocket
    confirm_timer: asyncio.Task[None] | None = None
    confirmation_s = confirmation_ms / 1000

    try:
        async for raw in ws:
            if isinstance(raw, bytes):
                continue
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "TurnInfo":
                event = msg.get("event")
                if event == "StartOfTurn":
                    # (Re)arm the confirmation window. If a prior
                    # window is still open it's superseded — the
                    # new StartOfTurn is the fresh evidence to
                    # confirm against.
                    if confirm_timer is not None and not confirm_timer.done():
                        confirm_timer.cancel()
                    confirm_timer = asyncio.create_task(
                        asyncio.sleep(confirmation_s),
                        name="stt-barge-in-confirm-timer",
                    )
                    _log.debug(
                        "stt(barge-in): StartOfTurn (turn_index=%r), confirm window=%dms",
                        msg.get("turn_index"),
                        confirmation_ms,
                    )
                    continue

                # Speech-indicative follow-up events only fire the
                # signal if the confirmation window is still open.
                if confirm_timer is None or confirm_timer.done():
                    continue

                if event == "Update":
                    transcript = msg.get("transcript", "") or ""
                    if not transcript:
                        continue  # heartbeat update — not evidence
                    _confirm(confirm_timer, barge_in_event, event, msg)
                    return
                if event in ("EagerEndOfTurn", "EndOfTurn", "TurnResumed"):
                    if event == "EndOfTurn":
                        transcript = msg.get("transcript", "") or ""
                        if not transcript:
                            # EndOfTurn with empty transcript — the
                            # server closed a non-speech turn.
                            # Disconfirming: cancel the window
                            # instead of confirming.
                            confirm_timer.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await confirm_timer
                            _log.debug(
                                "stt(barge-in): EndOfTurn with empty "
                                "transcript inside window; discarding "
                                "confirmation"
                            )
                            continue
                    _confirm(confirm_timer, barge_in_event, event, msg)
                    return

                # Other TurnInfo events: log and keep watching.
                _log.debug("stt(barge-in): non-salient TurnInfo event=%r", event)
                continue

            if msg_type == "Warning":
                code = msg.get("code")
                if code == "FORCE_END_TURN_NO_ACTIVE_TURN":
                    # Flux saw no active turn — the StartOfTurn
                    # (if any) was a flap. Discard pending window.
                    if confirm_timer is not None and not confirm_timer.done():
                        confirm_timer.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await confirm_timer
                        confirm_timer = None
                    _log.debug(
                        "stt(barge-in): FORCE_END_TURN_NO_ACTIVE_TURN; "
                        "discarding any pending confirmation"
                    )
                    continue
                _log.warning(
                    "stt(barge-in): Warning code=%r description=%r",
                    code,
                    msg.get("description"),
                )
                continue

            if msg_type == "Error":
                code = msg.get("code")
                if code == "UNPARSABLE_CLIENT_MESSAGE":
                    raise STTForceEndTurnUnsupported(
                        "ForceEndTurn not enabled on this Deepgram deployment "
                        "(FatalError code=UNPARSABLE_CLIENT_MESSAGE). "
                        "Enable ForceEndTurn on the Deepgram dashboard "
                        "(Feature Flags)."
                    )
                raise STTFatal(f"Flux fatal (code={code!r}): {msg.get('description')!r}")

            _log.debug("stt(barge-in): non-terminal type=%r", msg_type)
    except asyncio.CancelledError:
        raise
    except ConnectionClosed as e:
        raise STTError(f"barge-in connection closed: {e!r}") from e
    except (STTError, STTFatal, STTForceEndTurnUnsupported):
        raise
    except Exception as e:
        raise STTError(f"barge-in receive loop failed: {e!r}") from e
    finally:
        if confirm_timer is not None and not confirm_timer.done():
            confirm_timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await confirm_timer


def _confirm(
    confirm_timer: asyncio.Task[None],
    barge_in_event: asyncio.Event,
    event: str,
    msg: dict,
) -> None:
    """Fire the barge-in signal and cancel the pending window timer."""
    confirm_timer.cancel()
    _log.info(
        "stt(barge-in): confirmed via %s (turn_index=%r)",
        event,
        msg.get("turn_index"),
    )
    barge_in_event.set()


async def detect_barge_in(
    mic_chunks: AsyncIterator[bytes],
    barge_in_event: asyncio.Event,
    *,
    confirmation_ms: int = BARGEIN_CONFIRM_MS,
) -> None:
    """Open a Flux session scoped to SPEAKING and fire ``barge_in_event``
    on a confirmed ``StartOfTurn`` (ADR-0005 Decision 2).

    Args:
        mic_chunks: async iterator yielding 16 kHz mono s16le PCM
            bytes, same shape as :func:`transcribe` consumes. The
            caller holds the mic open through SPEAKING per ADR-0005
            Decision 4 and feeds it here as a sibling task to TTS
            playback.
        barge_in_event: shared ``asyncio.Event`` the state machine
            owns (see :attr:`agent.turn_state.TurnStateMachine.barge_in_event`).
            Set exactly once per invocation, on confirmed barge-in.
        confirmation_ms: confirmation-window duration in ms. Default
            :data:`BARGEIN_CONFIRM_MS` (150 ms).

    Runs until either (a) a barge-in is confirmed and ``barge_in_event``
    is set, (b) the mic iterator exhausts and the Flux session
    completes, or (c) the caller cancels this coroutine. Case (c) is
    the normal exit path at BARGE_IN entry or SPEAKING -> LISTENING
    clean end.

    Raises:
        STTAuthError: DEEPGRAM_API_KEY missing or invalid.
        STTForceEndTurnUnsupported: Flux rejected a (future)
            ForceEndTurn call with ``UNPARSABLE_CLIENT_MESSAGE``.
            ``detect_barge_in`` does not currently send ForceEndTurn,
            but mirrors ``transcribe``'s mapping for symmetry.
        STTFatal: Flux emitted a ``FatalError`` with any other code.
        STTError: any other pipeline failure.
    """
    try:
        client = _get_client()
    except ApiError as e:
        raise _wrap_api_error(e) from e
    except Exception as e:
        raise STTError(f"failed to construct Deepgram client: {e!r}") from e

    try:
        connect_cm = client.listen.v2.connect(
            model=MODEL,
            encoding=ENCODING,
            sample_rate=SAMPLE_RATE,
            eot_threshold=EOT_THRESHOLD,
            eager_eot_threshold=EAGER_EOT_THRESHOLD,
            eot_timeout_ms=EOT_TIMEOUT_MS,
        )
    except ApiError as e:
        raise _wrap_api_error(e) from e

    try:
        async with connect_cm as socket:
            # CR #86 F3: observe the pump as a sibling task via
            # asyncio.wait(FIRST_COMPLETED) rather than awaiting only
            # the watcher. Without this, a pump failure (OSError /
            # STTError wrap) while the watcher is blocked on
            # ``async for raw in ws`` with no pending messages would
            # leave the detector hanging until the caller cancels it.
            # In production voice_loop cancels on teardown so the hang
            # is bounded, but a future caller that expects the
            # detector to complete on its own (mic iterator exhaustion,
            # timeout-based teardown) would deadlock. Surface the
            # pump exception so it drives the exit.
            pump_task = asyncio.create_task(_mic_pump(socket, mic_chunks), name="stt-barge-in-pump")
            watcher_task = asyncio.create_task(
                _watch_for_barge_in(socket, barge_in_event, confirmation_ms),
                name="stt-barge-in-watcher",
            )
            try:
                done, _pending = await asyncio.wait(
                    {pump_task, watcher_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # If the pump finished first with a non-cancel exception,
                # raise it so the caller sees why the detector exited
                # instead of swallowing it only in the drain.
                if pump_task in done and not pump_task.cancelled():
                    exc = pump_task.exception()
                    if exc is not None and not isinstance(exc, asyncio.CancelledError):
                        raise exc
                # Watcher-first completion (confirmed barge-in or a
                # watcher-side raise) flows through the finally drain.
                if watcher_task in done and not watcher_task.cancelled():
                    w_exc = watcher_task.exception()
                    if w_exc is not None and not isinstance(w_exc, asyncio.CancelledError):
                        raise w_exc
            finally:
                for t in (pump_task, watcher_task):
                    if not t.done():
                        t.cancel()
                for t in (pump_task, watcher_task):
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
                    except STTError:
                        # Pump or watcher raised a transport failure
                        # during drain. If we're already propagating a
                        # typed exception from the wait() block above,
                        # that one wins; this WARNING just records that
                        # the sibling also died.
                        _log.warning(
                            "stt(barge-in): sibling task %s raised during drain; "
                            "receive-side outcome preserved",
                            t.get_name(),
                        )
                    except BaseException as e:  # noqa: BLE001 — defensive drain
                        _log.warning("stt(barge-in): sibling drain %s: %r", t.get_name(), e)
    except ApiError as e:
        raise _wrap_api_error(e) from e
    except (OSError, TimeoutError, WebSocketException) as e:
        raise STTError(f"barge-in connection failed: {e!r}") from e


# --- __main__ helpers ---------------------------------------------------
# The ``if __name__ == "__main__":`` dispatch below carries a
# ``# pragma: no cover`` per the precedent set in #48. The ``_main``
# coroutine itself is unit-tested with monkey-patched deps.


async def _main() -> None:
    """Wire audio.capture + ptt.events + transcribe, print the transcript.

    _main is single-turn by design; multi-turn loop belongs to #53.

    Watcher supervision (round-3 Finding 2): the PTT event stream can
    terminate before RELEASED (or before PRESSED) — cleanly or via an
    exception. If we naïvely awaited ``press_seen.wait()`` or the
    inner transcribe, we would hang forever in those cases. Both
    phases are guarded by ``asyncio.wait({watcher_task, phase_task},
    FIRST_COMPLETED)``: if the watcher terminates first with the phase
    condition unfulfilled, raise ``STTError`` and chain the watcher's
    exception via ``from`` when there was one.

    Waits for the first PRESSED before consuming any audio (round-1
    Finding 6) so nothing captured before the physical press can
    reach Deepgram.
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
        # Phase A: wait for PRESSED or watcher termination, whichever
        # comes first. Round-1 Finding 6: don't consume audio.capture()
        # before the physical press.
        press_wait = asyncio.create_task(press_seen.wait(), name="stt-main-press-wait")
        try:
            await asyncio.wait(
                {watcher_task, press_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not press_wait.done():
                press_wait.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await press_wait

        if not press_seen.is_set():
            # Watcher terminated before PRESSED. Chain its exception if any.
            exc = watcher_task.exception() if watcher_task.done() else None
            if exc is not None:
                raise STTError("PTT watcher terminated before PRESSED") from exc
            raise STTError("PTT watcher terminated before PRESSED")

        # Phase B: transcribe alongside the watcher. If the watcher
        # dies before setting ptt_release, transcribe would hang waiting
        # for a release that never comes — cancel it and raise instead
        # (Round-3 Finding 2). We do NOT let Flux's eot_timeout_ms fire
        # as a fallback: 30 s hangs on every watcher-death defeat the
        # purpose of the guard, and STTTimeout would misleadingly imply
        # the server backstop when the input pipeline actually collapsed.
        transcribe_task = asyncio.create_task(
            transcribe(audio.capture(), ptt_release),
            name="stt-main-transcribe",
        )
        await asyncio.wait(
            {watcher_task, transcribe_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if watcher_task.done() and not ptt_release.is_set():
            # Watcher finished (raise or clean return) without RELEASED.
            exc = watcher_task.exception()
            if not transcribe_task.done():
                transcribe_task.cancel()
                try:
                    await transcribe_task
                except asyncio.CancelledError:
                    pass
                except BaseException as e:  # noqa: BLE001
                    _log.warning(
                        "stt: task %s raised during post-error drain: %r",
                        transcribe_task.get_name(),
                        e,
                    )
            if exc is not None:
                raise STTError("PTT watcher terminated before RELEASED") from exc
            raise STTError("PTT watcher terminated before RELEASED")

        # Happy path (or watcher-completed-cleanly-after-RELEASED): await
        # transcribe to completion. Its own exceptions (STTAuthError,
        # STTTimeout, STTForceEndTurnUnsupported, STTFatal, STTError)
        # propagate unchanged.
        transcript = await transcribe_task
        print(f"[stt] transcript: {transcript!r}")
    finally:
        if not watcher_task.done():
            watcher_task.cancel()
        try:
            await watcher_task
        except asyncio.CancelledError:
            pass
        except BaseException as e:  # noqa: BLE001
            _log.warning(
                "stt: task %s raised during drain: %r",
                watcher_task.get_name(),
                e,
            )


if __name__ == "__main__":  # pragma: no cover
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        sys.exit(0)
