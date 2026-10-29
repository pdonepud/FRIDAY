"""Tier 3 conversation loops — text REPL + voice pipeline over Claude.

Two async coroutines drive user interaction. ``text_loop()`` preserves
the Tier-2 keyboard REPL (opened by ``python -m agent --text``);
``voice_loop()`` is the Tier-3 default push-to-talk pipeline composing
``agent.audio``, ``agent.ptt``, ``agent.stt``, ``agent.claude``, and
``agent.tts``. ``run()`` is the single entry point ``agent/__main__.py``
calls — it parses ``sys.argv`` with argparse, does shared startup
(``load_dotenv`` + per-branch API-key check), and dispatches to one
loop or the other, returning its int exit code.

The three streaming SDKs (anthropic, deepgram, elevenlabs) each sit
behind a dedicated seam module per ADR-0003; this file consumes those
public surfaces unchanged. Both loops share ``agent/claude.py`` as the
single LLM seam per ADR-0002: ``stream_tokens`` for text REPL
character-level streaming, ``stream_sentences`` for voice-path
sentence-chunking into TTS.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
from collections.abc import AsyncIterator

from dotenv import load_dotenv

from agent import audio, ptt, stt, tts
from agent.claude import (
    APIConnectionError,
    AuthenticationError,
    NetworkError,
    RateLimitError,
    stream_sentences,
    stream_tokens,
)
from agent.events import BargeInEvent
from agent.system_prompt import SYSTEM_PROMPT
from agent.turn_state import TurnState, TurnStateMachine

# --- Banners + stock messages -------------------------------------------
_BANNER: str = "FRIDAY — Tier 1 baseline. Type to talk. Ctrl+C to exit."
_VOICE_BANNER: str = "FRIDAY — voice mode. Hold Right Alt to talk. Ctrl+C to exit."
_MISSING_KEY: str = (
    "ANTHROPIC_API_KEY isn't set; see .env.example. Copy it to .env and add your key."
)
_GOODBYE: str = "\n[goodbye]"

# Env vars checked up front for the voice branch, in the order they're
# listed back to the user on a missing-key error. Order follows
# ``.env.example`` (alphabetical Anthropic → Deepgram → ElevenLabs).
_VOICE_REQUIRED_KEYS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "DEEPGRAM_API_KEY",
    "ELEVENLABS_API_KEY",
)


def _voice_missing_keys_message(missing: list[str]) -> str:
    """Compose the voice-branch missing-keys message per A4 of the plan.

    One ``print()`` body: each missing var's own
    ``<VAR> isn't set; see .env.example.`` line followed by a single
    ``Copy .env.example to .env and add your keys.`` footer. Mirrors
    the shape of ``_MISSING_KEY`` so text-mode and voice-mode UX read
    consistently.
    """
    lines = [f"{var} isn't set; see .env.example." for var in missing]
    lines.append("Copy .env.example to .env and add your keys.")
    return "\n".join(lines)


class _TranscriptStore:
    """Minimal transcript-store observer (#72 commit 3).

    Full persistence (#76 SQLite) lands later. This stub records the
    last turn's outcome and elapsed text so tests can assert the
    sealing decision at each state transition — in particular, the
    SPEAKING -> BARGE_IN edge (commit 4 fires it) must mark the
    assistant turn as ``"interrupted"`` with the elapsed sentence
    text, not ``"spoken"``.

    Fields:

    * ``last_outcome``: ``"spoken"`` on clean SPEAKING -> LISTENING;
      ``"interrupted"`` on SPEAKING -> BARGE_IN. ``None`` before the
      first SPEAKING-origin transition.
    * ``last_elapsed_text``: the sentence text accumulated into the
      TTS stream so far. ``voice_loop`` sets this imperatively before
      firing the SPEAKING-origin transition (text is a per-turn
      local, not something the observer can read from state alone).
    """

    def __init__(self) -> None:
        self.last_outcome: str | None = None
        self.last_elapsed_text: str | None = None

    def observe(
        self,
        from_state: TurnState,
        to_state: TurnState,
        trigger: str,
        payload: object,
    ) -> None:
        if from_state != TurnState.SPEAKING:
            return
        if to_state == TurnState.LISTENING:
            self.last_outcome = "spoken"
        elif to_state == TurnState.BARGE_IN:
            self.last_outcome = "interrupted"


class _TurnMetrics:
    """Minimal metrics observer (#72 commit 3).

    Counts transitions per ``(from, to)`` edge for diagnostics. Real
    metrics plumbing (OpenTelemetry etc.) is explicitly out of scope.
    """

    def __init__(self) -> None:
        self.transitions_seen: int = 0
        self.by_edge: dict[tuple[TurnState, TurnState], int] = {}

    def observe(
        self,
        from_state: TurnState,
        to_state: TurnState,
        trigger: str,
        payload: object,
    ) -> None:
        self.transitions_seen += 1
        key = (from_state, to_state)
        self.by_edge[key] = self.by_edge.get(key, 0) + 1


def _recover_to_listening_from_phase5(machine: TurnStateMachine, trigger: str = "manual") -> None:
    """Transition the machine back to LISTENING from a Phase 5
    intermediate state (THINKING, SPEAKING, or BARGE_IN) after a
    recoverable error. No-op if we're not in any — the error fired
    before THINKING was entered, or we never left LISTENING.
    """
    current = machine.current()
    if current == TurnState.THINKING:
        machine.transition(TurnState.LISTENING, trigger=trigger)
    elif current == TurnState.SPEAKING:
        machine.transition(TurnState.LISTENING, trigger=trigger)
    elif current == TurnState.BARGE_IN:
        # CR #86 F4: mid-aclose or mid-cancel after SPEAKING ->
        # BARGE_IN has fired but before the Phase-5 barged_in branch's
        # own BARGE_IN -> LISTENING transition ran. Without this
        # branch the machine would stay in BARGE_IN; the next
        # PRESSED would attempt an illegal BARGE_IN -> THINKING,
        # get rejected with a WARNING, and the stale ``barge_in_event``
        # (which the TurnStateMachine clears only on SPEAKING entry)
        # would fire the barge-in path on the next turn — a silent
        # lost-turn UX cliff. Recover here. The event stays set
        # until the next SPEAKING entry clears it per the commit-1
        # contract.
        machine.transition(TurnState.LISTENING, trigger=trigger)


async def _tee(src: AsyncIterator[str], collect: list[str]) -> AsyncIterator[str]:
    """Pass sentence chunks through to TTS while collecting them.

    Lifted to module scope (not a closure) so a reused ``collect``
    list belongs to each turn's local scope in ``voice_loop`` without
    tripping Ruff's B023 or confusing the GC.
    """
    async for chunk in src:
        collect.append(chunk)
        yield chunk


async def _mark_first_chunk(
    src: AsyncIterator[bytes],
    first_chunk_event: asyncio.Event,
) -> AsyncIterator[bytes]:
    """Pass PCM chunks through, setting ``first_chunk_event`` on the first chunk.

    #87: ``voice_loop`` wraps ``synth_iter`` with this helper so the
    ``THINKING -> SPEAKING`` transition fires only when playback
    actually observes its first audio chunk — not when the lazy
    generator is merely wired up. Lifted to module scope (not a
    closure) to match ``_tee``'s pattern and avoid B023.
    """
    first = True
    async for chunk in src:
        # CR #92 F1: tts.synthesize may yield an empty frame as the
        # first chunk (base64-decode of an empty audio field). Setting
        # the event here would spuriously enter SPEAKING with no
        # audible output. Only mark on the first NON-EMPTY chunk.
        if first and chunk:
            first_chunk_event.set()
            first = False
        yield chunk


async def _ptt_pump(
    ptt_iter: AsyncIterator[ptt.PTTEvent],
    ptt_queue: asyncio.Queue[ptt.PTTEvent | None],
    pump_error: list[BaseException],
) -> None:
    """Drain ``ptt.events()`` into a queue consumed by Phase 1 + watcher.

    Lives for the whole ``voice_loop`` lifetime. The queue decouples
    the single shared async generator from its two per-turn consumers
    (Phase 1 idle wait, ``_watch_release``) so cancelling a consumer
    only cancels its ``queue.get()`` — the generator's ``finally``
    (which stops the pynput Listener) never fires mid-session.

    On any pump exit path (iterator exhausted, exception, cancellation)
    a ``None`` sentinel is enqueued so a consumer blocked on ``get()``
    wakes and sees pump termination. If the iterator raised, the
    exception is stashed in the ``pump_error`` out-param (CancelledError
    excluded — it re-raises so outer-finally teardown recognizes it) so
    Phase 1 can surface the original ``[ptt] <Type>: <msg>`` detail
    rather than the generic "event stream ended" line.
    """
    try:
        async for event in ptt_iter:
            await ptt_queue.put(event)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — PTT death is fatal (A2)
        pump_error.append(e)
    finally:
        # Best-effort sentinel; swallow failure (queue full is
        # impossible with the single-consumer-at-a-time design here,
        # but defensive).
        with contextlib.suppress(Exception):
            ptt_queue.put_nowait(None)


async def _watch_release(
    ptt_queue: asyncio.Queue[ptt.PTTEvent | None],
    ptt_release: asyncio.Event,
) -> None:
    """Wait on the pumped queue for the next RELEASED (or pump death).

    Sets ``ptt_release`` as soon as RELEASED arrives so
    ``stt.transcribe`` can finalize via its ForceEndTurn path. On any
    exit path (RELEASED seen, pump sentinel ``None`` seen, exception
    propagated, task cancelled) the Event is set in the ``finally``
    block so a pending transcribe never hangs forever on PTT death —
    the next Phase 1 PRESSED-wait will detect the sentinel and return 2
    per the fatal-triage table.
    """
    try:
        while True:
            event = await ptt_queue.get()
            if event is None or event == ptt.PTTEvent.RELEASED:
                return
            # Defensive: an unexpected PRESSED (orphaned pair, etc.)
            # while watching for RELEASED — ignore and keep listening.
    finally:
        ptt_release.set()


async def text_loop() -> int:
    """Tier-2-preserved keyboard REPL. ``run()`` dispatches here on ``--text``.

    Exit codes:
        0 — Ctrl+C or EOF at the prompt.
        2 — unrecoverable auth failure mid-session.

    Assumes ``run()`` has already loaded ``.env`` and verified
    ``ANTHROPIC_API_KEY`` is present.
    """
    print(_BANNER)
    messages: list[dict] = []

    while True:
        # --- prompt for a user turn ----------------------------------
        try:
            # sync input() is deliberate — text REPL has no concurrent
            # coroutines and pauses on stdin by design.
            user = input("you > ").strip()
        except (KeyboardInterrupt, EOFError):
            print(_GOODBYE)
            return 0
        if not user:
            continue

        # --- send it and stream the reply ----------------------------
        messages.append({"role": "user", "content": user})
        print("friday > ", end="", flush=True)
        chunks: list[str] = []
        try:
            async for chunk in stream_tokens(messages, SYSTEM_PROMPT):
                print(chunk, end="", flush=True)
                chunks.append(chunk)
            print()
            messages.append({"role": "assistant", "content": "".join(chunks)})
        except (KeyboardInterrupt, asyncio.CancelledError):
            # #55 D3 (R4): mid-stream Ctrl+C. Under asyncio.run (the
            # only driver today) SIGINT delivers via main-task
            # cancellation → CancelledError at the next await inside
            # stream_tokens. KeyboardInterrupt is kept defensively for
            # alternate drivers that don't install Runner's SIGINT
            # handler. Text REPL users expect Ctrl+C to end the
            # session (not interrupt the current token stream); the
            # "interrupt what Friday is saying" UX lives in voice
            # mode's Phase 5 handler.
            print(_GOODBYE)
            return 0
        except AuthenticationError:
            print("\n[auth] ANTHROPIC_API_KEY isn't working. Check your .env and restart.")
            return 2
        except RateLimitError:
            print("\n[rate] Hit the rate limit — give it a moment.")
            messages.pop()
        except (APIConnectionError, NetworkError):
            # NetworkError covers mid-stream httpx2.TransportError
            # drops the SDK does NOT translate into APIConnectionError
            # (see agent/claude.py; verified empirically in #55 D4).
            print("\n[net] Can't reach Claude right now — check your connection.")
            messages.pop()
        except Exception as e:  # noqa: BLE001 — final safety net for text loop
            print(f"\n[err] {type(e).__name__}: {e}")
            messages.pop()


async def voice_loop() -> int:
    """Tier-3 push-to-talk pipeline. ``run()`` dispatches here by default.

    One turn: PRESSED → ``audio.capture`` → ``stt.transcribe`` →
    (empty? short-circuit) → ``claude.stream_sentences`` →
    ``tts.synthesize`` → ``audio.playback`` → back to PRESSED wait.
    One ``ptt.events()`` iterator is held for the whole loop and
    drained by a long-lived ``_ptt_pump`` task into an
    ``asyncio.Queue``; a per-turn ``_watch_release`` task consumes
    the queue to catch RELEASED and is torn down at turn end. The
    pump sits between the generator and its consumers so that
    cancelling the per-turn watcher does not run the generator's
    ``finally`` (which would stop the pynput Listener mid-session).

    Exit codes:
        0 — Ctrl+C / cancellation at the idle wait between turns.
        2 — fatal auth across any provider, or the PTT event stream
            dies / exhausts (continuing would tight-loop into the
            same error on the next ``_next_press`` call).

    Assumes ``run()`` has already loaded ``.env`` and verified all
    three keys are present.
    """
    print(_VOICE_BANNER)
    messages: list[dict] = []
    # #72 commit 3: voice_loop is the single owner of the state
    # machine. Observers (transcript store, metrics) register at
    # setup and never fire or hold state outside their own objects.
    # Commit 4 wires detect_barge_in to the machine's barge_in_event
    # and introduces the SPEAKING -> BARGE_IN transition; commit 3
    # keeps behaviour identical to the pre-refactor loop.
    machine = TurnStateMachine()
    transcript_store = _TranscriptStore()
    turn_metrics = _TurnMetrics()
    machine.on_change(transcript_store.observe)
    machine.on_change(turn_metrics.observe)
    ptt_iter = ptt.events()
    ptt_queue: asyncio.Queue[ptt.PTTEvent | None] = asyncio.Queue(
        maxsize=ptt.EVENT_QUEUE_MAX,
    )
    pump_error: list[BaseException] = []
    pump_task = asyncio.create_task(
        _ptt_pump(ptt_iter, ptt_queue, pump_error),
        name="voice-loop-ptt-pump",
    )

    try:
        while True:
            # --- Phase 1: idle wait for PRESSED --------------------
            try:
                while True:
                    event = await ptt_queue.get()
                    if event is None:
                        # Pump sentinel: iterator exhausted or raised.
                        # NOTE: #55 may want structured PTT-health
                        # handling here; for now, PTT death is fatal
                        # (A2 in #53 plan).
                        if pump_error:
                            e = pump_error[0]
                            print(f"[ptt] {type(e).__name__}: {e}")
                        else:
                            print("[ptt] event stream ended at idle wait")
                        return 2
                    if event == ptt.PTTEvent.PRESSED:
                        break
                    # Orphan RELEASED at idle — defensive, keep listening.
            except (KeyboardInterrupt, asyncio.CancelledError):
                # SIGINT at idle. On Python 3.11+, ``asyncio.run()``
                # delivers SIGINT by cancelling the main task, so the
                # Phase 1 ``await`` sees CancelledError, not KI. We
                # treat both as clean idle exits. Mid-stream Phase 5
                # cancellation (interrupt-the-response UX) is a
                # different concern tracked under #55.
                print(_GOODBYE)
                return 0
            except Exception as e:  # noqa: BLE001 — PTT death is fatal (A2)
                # NOTE: #55 may introduce structured PTT-health handling
                # (restart listener? exit?) — right now any PTT iterator
                # exception is terminal to avoid tight error loops.
                print(f"[ptt] {type(e).__name__}: {e}")
                return 2

            # #72 commit 3: first PRESSED of the session takes the
            # machine out of IDLE. Later turns stay in LISTENING
            # between press/release cycles (SPEAKING -> LISTENING at
            # Phase 5 clean end is the resting-state return path), so
            # the IDLE -> LISTENING transition fires exactly once per
            # voice_loop entry. The ``if current == IDLE`` guard
            # keeps subsequent PRESSEDs from attempting an illegal
            # self-transition.
            if machine.current() == TurnState.IDLE:
                machine.transition(TurnState.LISTENING, trigger="loop")

            # Spawn the per-turn RELEASED watcher. Watcher consumes the
            # pumped queue, so cancelling it mid-``get()`` only
            # cancels its own task — the shared ``ptt.events()``
            # generator stays alive for the next turn.
            ptt_release = asyncio.Event()
            watcher_task = asyncio.create_task(
                _watch_release(ptt_queue, ptt_release),
                name="voice-loop-ptt-watcher",
            )

            try:
                # --- Phase 2: transcribe ---------------------------
                try:
                    transcript = await stt.transcribe(audio.capture(), ptt_release)
                except (stt.STTAuthError, stt.STTForceEndTurnUnsupported) as e:
                    print(f"\n[auth] {e}")
                    return 2
                except stt.STTTimeout:
                    print("\n[stt timeout]")
                    continue
                except (stt.STTFatal, stt.STTError) as e:
                    print(f"\n[stt] {e}")
                    continue

                # --- Phase 3: empty-transcript short-circuit -------
                if transcript == "":
                    print("[no speech]")
                    continue

                # --- Phase 4: user turn append ---------------------
                messages.append({"role": "user", "content": transcript})

                # --- Phase 5: Claude + TTS + playback --------------
                collect: list[str] = []
                # #72 commit 3: LISTENING -> THINKING at Phase 5
                # entry (LLM call about to start). #72 commit 4
                # (ADR-0005 Decision 4): THINKING -> SPEAKING spawns
                # a sibling ``detect_barge_in`` task on the open mic;
                # ``asyncio.wait`` races playback completion against
                # the state-machine's ``barge_in_event``. SPEAKING ->
                # LISTENING fires on clean end; SPEAKING ->
                # BARGE_IN -> LISTENING on confirmed barge-in (TTS
                # cancel plumbing is #71 — commit 4 stubs it via
                # ``playback_task.cancel()``). Error paths route back
                # to LISTENING via _recover_to_listening_from_phase5.
                #
                # TODO(#72-echo): the barge-in detector currently
                # relies on commit 2's confirmation window as the
                # only echo-filter. Flux doesn't surface a per-event
                # confidence we can gate on. Real-world testing with
                # Lily's output into an open mic may show self-echo
                # confirming spuriously; if so, file a follow-up to
                # add a feature flag defaulting to the mic-closed
                # behaviour (current pre-#72-commit-4) and ship the
                # mic-open flip behind it when EC lands.
                machine.transition(TurnState.THINKING, trigger="loop")
                barged_in = False
                # #87: on the empty-stream path (LLM produced nothing
                # speakable) we take THINKING -> LISTENING directly,
                # pop the pending user turn, and print [no reply].
                # Flagged here, acted on after the inner finally so
                # the cleanup block runs on BOTH branches uniformly.
                no_reply = False
                # Pre-init so the widened inner finally can guard
                # cleanup with ``is not None`` — barge_in_mic +
                # detect_task + barge_in_waiter are only created on
                # the SPEAKING path, phase5_tasks stays () on the
                # empty-stream path (its drain loop is then a no-op).
                barge_in_mic = None
                detect_task: asyncio.Task | None = None
                barge_in_waiter: asyncio.Task | None = None
                phase5_tasks: tuple[asyncio.Task, ...] = ()
                first_chunk_waiter: asyncio.Task | None = None
                # CR #92 F2: pre-declare so the inner finally can
                # cancel+await it on a SIGINT (or any exception)
                # during the pre-roll window — before it joins
                # phase5_tasks on the SPEAKING branch.
                playback_task: asyncio.Task | None = None
                try:
                    sentence_iter = stream_sentences(messages, SYSTEM_PROMPT)
                    synth_iter = tts.synthesize(_tee(sentence_iter, collect))

                    # #87: THINKING -> SPEAKING must fire only when
                    # audio.playback consumes its first chunk — not
                    # when the lazy generator chain is merely wired
                    # up. If we transitioned here, the barge-in
                    # detector + barge_in_waiter would already be
                    # running during the LLM + TTS pre-roll (no
                    # audio yet) and a Flux ``StartOfTurn`` from the
                    # open mic could confirm and discard a turn the
                    # user never actually heard. Addresses CR #86
                    # F1 / ADR-0005's "barge-in detector must not
                    # run during the pre-audio THINKING window"
                    # invariant.
                    first_chunk_event = asyncio.Event()
                    wrapped_synth = _mark_first_chunk(synth_iter, first_chunk_event)
                    playback_task = asyncio.create_task(
                        audio.playback(wrapped_synth),
                        name="voice-loop-playback",
                    )
                    first_chunk_waiter = asyncio.create_task(
                        first_chunk_event.wait(),
                        name="voice-loop-first-chunk-waiter",
                    )
                    try:
                        pre_done, _pre_pending = await asyncio.wait(
                            {first_chunk_waiter, playback_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if first_chunk_waiter not in pre_done:
                            # Empty-stream path: audio.playback
                            # consumed the whole synth iterator
                            # without observing a chunk, OR it
                            # raised before any chunk arrived.
                            # Drain the waiter inline so the inner
                            # finally's guard sees ``done()`` and
                            # skips re-cancel.
                            if not first_chunk_waiter.done():
                                first_chunk_waiter.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await first_chunk_waiter
                            # If playback raised, surface it — the
                            # outer Phase-5 typed-exception handlers
                            # own the recovery (net / tts / etc).
                            if playback_task.exception() is not None:
                                raise playback_task.exception()
                            # Genuine empty stream — "LLM produced
                            # nothing speakable" per #87 + ADR-0005.
                            # The (THINKING, LISTENING) edge is
                            # already in ``_LEGAL_TRANSITIONS``
                            # (agent/turn_state.py:132). Pop + continue
                            # after the finally runs — role-alternation
                            # parity with the SIGINT / net / tts
                            # recoverable aborts below (L583-644 of
                            # pre-#87 loop.py).
                            no_reply = True
                            machine.transition(TurnState.LISTENING, trigger="loop")
                        else:
                            # First chunk observed — Lily is actually
                            # about to make sound. Transition for
                            # real and spawn the mic + detector +
                            # waiter.
                            machine.transition(TurnState.SPEAKING, trigger="loop")

                            # Hoist the mic generator so we can
                            # explicitly ``aclose()`` it in the
                            # finally after cancelling the detector
                            # task. ``async for`` inside ``_mic_pump``
                            # does NOT call ``aclose()`` on
                            # cancellation, so the ``RawInputStream``
                            # held by ``audio.capture()`` would stay
                            # open until the async generator's
                            # finalization runs — unbounded delay on
                            # Windows. Mirrors the
                            # ``ptt_iter.aclose()`` pattern in
                            # voice_loop's outer finally.
                            barge_in_mic = audio.capture()
                            detect_task = asyncio.create_task(
                                stt.detect_barge_in(barge_in_mic, machine.barge_in_event),
                                name="voice-loop-barge-in-detector",
                            )
                            barge_in_waiter = asyncio.create_task(
                                machine.barge_in_event.wait(),
                                name="voice-loop-barge-in-waiter",
                            )
                            phase5_tasks = (detect_task, playback_task, barge_in_waiter)
                            done, _pending = await asyncio.wait(
                                {playback_task, barge_in_waiter},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if barge_in_waiter in done:
                                # Barge-in: fire SPEAKING -> BARGE_IN
                                # with the BargeInEvent payload
                                # observers need (metrics, transcript
                                # store).
                                barged_in = True
                                transcript_store.last_elapsed_text = "".join(collect)
                                machine.transition(
                                    TurnState.BARGE_IN,
                                    trigger="flux",
                                    payload=BargeInEvent(
                                        at=asyncio.get_running_loop().time(),
                                        source="flux",
                                        confidence=None,
                                    ),
                                )
                                # Cancel playback as the (#71-stubbed)
                                # TTS cancel. #71 replaces this with
                                # the destructive-cancel ADR-0005
                                # Decision 3 specifies.
                                if not playback_task.done():
                                    playback_task.cancel()
                            else:
                                # Clean playback end — surface any
                                # error the playback task raised
                                # (would be a Phase-5-handled
                                # exception type).
                                if playback_task.exception() is not None:
                                    raise playback_task.exception()
                                transcript_store.last_elapsed_text = "".join(collect)
                                machine.transition(TurnState.LISTENING, trigger="loop")
                    finally:
                        # CR #92 F2: pre-roll playback_task cleanup —
                        # on a SIGINT (or any exception) between
                        # ``playback_task = create_task(...)`` and
                        # phase5_tasks being assigned on the SPEAKING
                        # branch, playback_task would otherwise leak
                        # and keep consuming its synth stream across
                        # turns. ``not in phase5_tasks`` guard skips
                        # the double-cleanup on the SPEAKING path
                        # where the drain loop below already handles
                        # it.
                        if (
                            playback_task is not None
                            and not playback_task.done()
                            and playback_task not in phase5_tasks
                        ):
                            playback_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError, Exception):
                                await playback_task
                        # #87: first_chunk_waiter cleanup — guarded by
                        # ``is not None`` so an exception before its
                        # create_task doesn't trip, and ``not done()``
                        # so the empty-stream path (which already
                        # drained it inline) + the normal path (where
                        # it fired naturally) both avoid double-cancel.
                        if first_chunk_waiter is not None and not first_chunk_waiter.done():
                            first_chunk_waiter.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await first_chunk_waiter
                        # Drain phase5_tasks on the SPEAKING path.
                        # Empty-stream path leaves phase5_tasks as
                        # () so this loop is a no-op there. detect_task
                        # exceptions are swallowed with WARNING — a
                        # dead Flux session during SPEAKING is
                        # recoverable (user hears Lily, loses only
                        # the barge-in capability for that turn).
                        for t in phase5_tasks:
                            if not t.done():
                                t.cancel()
                        for t in phase5_tasks:
                            try:
                                await t
                            except asyncio.CancelledError:
                                pass
                            except Exception as e:  # noqa: BLE001 — defensive
                                if t is detect_task:
                                    print(f"[bargein] detector raised: {e!r}")
                                # playback_task / barge_in_waiter
                                # exceptions have already been
                                # surfaced via the done-set check or
                                # cancel propagation.
                        # Explicit aclose so the RawInputStream inside
                        # audio.capture() releases now, not at async-
                        # generator finalization time (which can be
                        # unbounded on Windows). CodeRabbit #86 F2.
                        # Empty-stream path never calls audio.capture(),
                        # so guard with ``is not None``.
                        if barge_in_mic is not None:
                            with contextlib.suppress(Exception):
                                await barge_in_mic.aclose()
                    if no_reply:
                        # #87: LLM produced nothing speakable. Drop
                        # the pending Phase-4 user turn + continue
                        # — role-alternation parity with the SIGINT,
                        # net-error, and tts-error recoverable
                        # abort paths further down this except
                        # chain.
                        print("[no reply]")
                        messages.pop()
                        continue
                    if barged_in:
                        # BARGE_IN -> LISTENING follows the cancel.
                        # #71 will own the ordering with the real
                        # destructive-cancel plumbing; here it fires
                        # after the finally drains the sibling tasks.
                        machine.transition(TurnState.LISTENING, trigger="loop")
                        print("\n[barged in]")
                        # Discard the pending user turn (option a).
                        # Phase 6 has NOT run (we never reached the
                        # clean path), so ``messages`` currently ends
                        # with the Phase-4-appended user turn and no
                        # assistant counterpart. Popping it matches
                        # the SIGINT-stopped and other per-turn-abort
                        # paths; the alternative — keep user, append
                        # the partial assistant reply from ``collect``
                        # — is a UX-nicer "preserve mid-utterance
                        # context" design but belongs in a focused
                        # follow-up PR with its own ADR consideration
                        # (how to mark the partial, interaction with
                        # ADR-0007's context-rot semantics). Leaving
                        # user alive without a popped-or-appended
                        # assistant would violate Anthropic's role-
                        # alternation requirement on the next turn.
                        messages.pop()
                        continue
                except asyncio.CancelledError:
                    # #55 D3: mid-response Ctrl+C. Under asyncio.run
                    # SIGINT delivers as task cancellation → CancelledError
                    # at the next await inside Phase 5. We check
                    # current_task().uncancel(): if remaining > 0 some
                    # OTHER source (TaskGroup / wait_for / direct
                    # task.cancel) still wants us cancelled — don't
                    # swallow. Zero means it was just our SIGINT and
                    # we treat it as a per-turn abort: stop speaking,
                    # drop the pending user turn, stay in the loop.
                    # Second Ctrl+C still exits because Runner's
                    # _interrupt_count goes to 2 and raises KI directly.
                    task = asyncio.current_task()
                    remaining = task.uncancel() if task is not None else 1
                    if remaining > 0:
                        raise
                    print("\n[stopped]")
                    _recover_to_listening_from_phase5(machine)
                    messages.pop()
                    continue
                except AuthenticationError:
                    # #55 D6: name the env var so the user knows which
                    # key to check in .env (parity with Deepgram/
                    # ElevenLabs messages below).
                    print("\n[auth] ANTHROPIC_API_KEY isn't working. Check your .env and restart.")
                    return 2  # A1: fatal, do NOT pop (match text_loop)
                except tts.TTSAuthError as e:
                    print(f"\n[auth] {e}")
                    return 2  # A1: fatal
                except RateLimitError:
                    print("\n[rate] Hit the rate limit — give it a moment.")
                    _recover_to_listening_from_phase5(machine)
                    messages.pop()
                    continue
                except (APIConnectionError, NetworkError):
                    # #55 D4: NetworkError covers mid-stream
                    # httpx2.TransportError drops the Anthropic SDK
                    # does NOT translate (verified empirically — see
                    # agent/claude.py). APIConnectionError still fires
                    # at connect-time.
                    print("\n[net] Lost connection mid-response — try again.")
                    _recover_to_listening_from_phase5(machine)
                    messages.pop()
                    continue
                except audio.AudioPlaybackError as e:
                    # #55 D1: output device failed mid-stream. The
                    # sentences the _tee collected so far ARE the
                    # text the TTS seam tried to speak — print them
                    # as a text fallback so the user still sees
                    # Claude's reply even without audio.
                    print(f"\n[audio] {e}")
                    _recover_to_listening_from_phase5(machine)
                    full_reply = "".join(collect).strip()
                    if full_reply:
                        # R5-locked: the user saw this text, so KEEP
                        # the assistant turn on messages. Popping
                        # here would desync the conversation ("why
                        # doesn't she remember what she just told me?")
                        # on the user's next turn. Comment must stay —
                        # explains intent for future refactors.
                        print(f"friday > {full_reply}")
                        messages.append({"role": "assistant", "content": full_reply})
                    else:
                        # No sentence emitted before the device died;
                        # drop the pending user turn and let them retry.
                        messages.pop()
                    continue
                except tts.TTSError as e:
                    # #55 D2: TTS stream died. Don't retry (circular +
                    # burns credits). Text-only apology instead.
                    print(f"\n[tts] {e}")
                    print("[friday] Sorry, I lost my voice. Try again.")
                    _recover_to_listening_from_phase5(machine)
                    messages.pop()
                    continue
                except Exception as e:  # noqa: BLE001 — final safety net for voice loop
                    print(f"\n[err] {type(e).__name__}: {e}")
                    _recover_to_listening_from_phase5(machine)
                    messages.pop()
                    continue

                # --- Phase 6: assistant turn append (clean path) ---
                messages.append({"role": "assistant", "content": "".join(collect)})
            finally:
                # Tear down the per-turn watcher so it doesn't eat the
                # next PRESSED off the pumped queue. On RELEASED-seen
                # the watcher has already returned; this cancel is a
                # no-op in that case. On any mid-turn exit (recoverable
                # or fatal) the watcher may still be blocked on
                # ``ptt_queue.get()`` — cancel + await. Because the
                # watcher consumes the queue (not the shared
                # ``ptt.events()`` generator directly), cancellation
                # here does NOT close the generator, so the next turn's
                # Phase 1 still sees live PTT events.
                if not watcher_task.done():
                    watcher_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await watcher_task
    finally:
        # Tear down the pump before closing the iterator so pynput's
        # Listener stops cleanly on any exit path. Cancellation on the
        # pump is safe here because ``_ptt_pump`` is the only task
        # still consuming the shared ``ptt.events()`` generator at
        # this point. ``agent.ptt.events`` closes idempotently via
        # its own finally.
        pump_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pump_task
        with contextlib.suppress(Exception):
            await ptt_iter.aclose()


async def run() -> int:
    """Entry point for ``python -m agent``. Parses argv and dispatches.

    Flags:
        --text   Run the text REPL instead of the voice pipeline.

    Exit codes:
        0 — clean exit (Ctrl+C at prompt / idle, EOF).
        1 — startup failure (missing API key).
        2 — unrecoverable auth / fatal provider error mid-session.
    """
    # Force UTF-8 on stdout so the em dash in both banners and any
    # non-ASCII in Claude's replies render correctly on Windows
    # consoles (which otherwise default to the system ANSI code page).
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — best-effort; a stdout without
        pass  # reconfigure (rare) still works for ASCII.

    parser = argparse.ArgumentParser(
        prog="python -m agent",
        description="FRIDAY — voice-first conversation loop.",
    )
    parser.add_argument(
        "--text",
        action="store_true",
        help="Run the keyboard REPL instead of the voice pipeline.",
    )
    args = parser.parse_args()

    load_dotenv()

    if args.text:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print(_MISSING_KEY)
            return 1
        return await text_loop()

    missing = [key for key in _VOICE_REQUIRED_KEYS if not os.environ.get(key)]
    if missing:
        print(_voice_missing_keys_message(missing))
        return 1

    # #55 D5: startup probes before the voice banner. On device
    # unavailability, fall back to text mode instead of trying to open
    # a stream that will tight-loop into [stt chunk source failed] on
    # every turn. ANTHROPIC_API_KEY is already present from the voice
    # key check above, so text_loop's own key check is a no-op.
    try:
        audio.probe_input_device()
        audio.probe_output_device()
    except audio.AudioDeviceUnavailable as e:
        print(f"[audio] {e}; falling back to text mode.")
        return await text_loop()

    return await voice_loop()
