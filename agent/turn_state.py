"""Voice turn state machine (#72, ADR-0005 Decision 4).

Single owner of voice turn state. ``voice_loop`` and the per-turn
tasks observe transitions via :meth:`TurnStateMachine.on_change`;
nothing else writes state.

## States

``IDLE → LISTENING → THINKING → SPEAKING ↔ BARGE_IN → LISTENING``

* ``IDLE`` — pre-first-turn and the brief gap after the loop exits.
  Not re-entered during the session except on disconnect/timeout.
* ``LISTENING`` — PTT held, Flux WebSocket open, mic active, audio
  flowing into STT.
* ``THINKING`` — transcript locked, LLM call in flight, no audio
  being produced yet. Entered only when a non-empty transcript lands
  and the LLM call actually starts; empty-transcript PTT presses
  short-circuit at the loop level and stay in LISTENING without any
  transition (see Implementation notes on #72).
* ``SPEAKING`` — TTS is playing. Mic stays open (ADR-0005 Decision 4)
  so Flux can detect a ``StartOfTurn`` for barge-in.
* ``BARGE_IN`` — ``StartOfTurn`` confirmed during SPEAKING; TTS
  cancels, playback drains, machine returns to LISTENING. This is
  the only "soft cancel" state; hard failures (watchdog, provider
  error) exit SPEAKING directly to LISTENING via a clean turn end.

## Legal transitions (encoded in ``_LEGAL_TRANSITIONS``)

* ``IDLE → LISTENING``
* ``LISTENING → THINKING``
* ``LISTENING → IDLE`` (disconnect / timeout)
* ``THINKING → SPEAKING``
* ``THINKING → LISTENING`` (LLM produced nothing speakable — distinct
  from the empty-transcript short-circuit which never enters
  THINKING in the first place)
* ``SPEAKING → BARGE_IN``
* ``SPEAKING → LISTENING`` (clean turn end)
* ``BARGE_IN → LISTENING``

Everything else — including self-transitions like
``BARGE_IN → BARGE_IN`` on a re-emitted signal — is illegal. Illegal
transitions log a WARNING with ``(from, to, trigger)``, leave state
unchanged, and do not fire observers.

## Barge-in signal

``SPEAKING → BARGE_IN`` sets the public :attr:`barge_in_event`
``asyncio.Event`` so a concurrent playback task can
``await barge_in_event.wait()`` to cancel. The event is cleared on
entry to SPEAKING (fresh signal slot per SPEAKING period) so a stale
signal from a previous turn cannot pre-fire.

Observers additionally receive the structured ``payload`` passed to
:meth:`transition` — typically an :class:`agent.events.BargeInEvent`
dataclass carrying monotonic timestamp, signal source, and
confidence for metrics / transcript store / structured log use. The
``asyncio.Event`` handles async coordination (what the TTS cancel in
#71 awaits); the dataclass carries metadata. ADR-0005 Decision 3
left this split open at ADR time; the dual pattern is defensible by
the async coordination need #71 will have.

## Observer error isolation

Each observer callback is invoked inside its own ``try/except``.
A raising observer logs a WARNING naming the callback and the
exception, then the loop continues firing subsequent observers.
State consistency is preserved. One misbehaving observer must not
corrupt state or break the voice loop.

## Trigger values

The ``trigger`` arg is first-class — logged on every transition,
tested against the canonical set. Valid values:

* ``"model"`` — Flux ``trigger="model"`` natural end-of-turn
* ``"manual"`` — loop-driven after a Phase-5 error recovery (chosen
  so logs separate "something went wrong, back to resting" from
  normal flow)
* ``"loop"`` — ``voice_loop`` drove this transition at a phase
  boundary (clean turn progression: IDLE→LISTENING on first PRESS,
  LISTENING→THINKING at LLM start, THINKING→SPEAKING at playback
  start, SPEAKING→LISTENING on clean end, BARGE_IN→LISTENING after
  cancel completes)
* ``"vad"`` — voice-activity-detector signal (reserved for future)
* ``"flux"`` — Flux-emitted event (StartOfTurn confirmation,
  EagerEndOfTurn, TurnResumed). SPEAKING→BARGE_IN fires with this
  trigger from the ``detect_barge_in`` path.
* ``"timeout"`` — timeout / disconnect
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from enum import Enum
from typing import Any, Literal

_log = logging.getLogger(__name__)

__all__ = [
    "Observer",
    "TRIGGERS",
    "Trigger",
    "TurnState",
    "TurnStateMachine",
]


class TurnState(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    BARGE_IN = "barge_in"


Trigger = Literal["model", "manual", "loop", "vad", "flux", "timeout"]
TRIGGERS: tuple[str, ...] = ("model", "manual", "loop", "vad", "flux", "timeout")

Observer = Callable[[TurnState, TurnState, str, Any], None]


# Legal transition set, encoded exactly as #72 dictates. See module
# docstring for the rationale on each edge.
_LEGAL_TRANSITIONS: frozenset[tuple[TurnState, TurnState]] = frozenset(
    {
        (TurnState.IDLE, TurnState.LISTENING),
        (TurnState.LISTENING, TurnState.THINKING),
        (TurnState.LISTENING, TurnState.IDLE),
        (TurnState.THINKING, TurnState.SPEAKING),
        (TurnState.THINKING, TurnState.LISTENING),
        (TurnState.SPEAKING, TurnState.BARGE_IN),
        (TurnState.SPEAKING, TurnState.LISTENING),
        (TurnState.BARGE_IN, TurnState.LISTENING),
    }
)


class TurnStateMachine:
    """Single owner of voice turn state.

    Public surface:

    * :meth:`current` — current :class:`TurnState`.
    * :meth:`transition` — attempt a transition; returns True if
      accepted, False if illegal.
    * :meth:`on_change` — register an observer callback.
    * :attr:`barge_in_event` — ``asyncio.Event`` fired on
      ``SPEAKING → BARGE_IN`` for async coordination with the
      playback cancel path.
    """

    def __init__(self) -> None:
        self._state: TurnState = TurnState.IDLE
        self._observers: list[Observer] = []
        self.barge_in_event: asyncio.Event = asyncio.Event()

    def current(self) -> TurnState:
        """Return the current state."""
        return self._state

    def on_change(self, callback: Observer) -> None:
        """Register ``callback`` to receive ``(from, to, trigger, payload)``
        on every accepted transition.

        Observers are invoked in registration order. One observer
        raising does not break the dispatch loop (see module
        docstring, "Observer error isolation").
        """
        self._observers.append(callback)

    def transition(
        self,
        to: TurnState,
        trigger: Trigger,
        payload: Any = None,
    ) -> bool:
        """Attempt a transition to ``to``.

        Returns ``True`` if the ``(current, to)`` pair is in
        :data:`_LEGAL_TRANSITIONS`; the state is updated, the
        barge-in event is managed (set on ``SPEAKING → BARGE_IN``;
        cleared on entry to ``SPEAKING``), and observers fire with
        ``(from, to, trigger, payload)``.

        Returns ``False`` for illegal transitions (including
        self-transitions): a WARNING is logged with
        ``(from, to, trigger)``; state is unchanged; no observer
        fires; no exception is raised.
        """
        from_state = self._state
        if (from_state, to) not in _LEGAL_TRANSITIONS:
            _log.warning(
                "turn_state: illegal transition rejected: from=%s to=%s trigger=%s",
                from_state.name,
                to.name,
                trigger,
            )
            return False

        self._state = to

        # Fresh signal slot per SPEAKING period so a stale signal
        # from a previous turn cannot pre-fire the next.
        if to == TurnState.SPEAKING:
            self.barge_in_event.clear()
        # SPEAKING → BARGE_IN is the one edge that sets the event.
        if from_state == TurnState.SPEAKING and to == TurnState.BARGE_IN:
            self.barge_in_event.set()

        for obs in self._observers:
            try:
                obs(from_state, to, trigger, payload)
            except Exception as e:  # noqa: BLE001 — observer failures must not corrupt state
                _log.warning(
                    "turn_state: observer %r raised: %r (state consistency preserved, "
                    "subsequent observers still fired)",
                    obs,
                    e,
                )
        return True
