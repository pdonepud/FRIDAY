"""Structured event payloads for the voice turn state machine (#72).

These dataclasses travel on the ``payload`` arg of
:meth:`agent.turn_state.TurnStateMachine.transition` and are forwarded
to observers so metrics, structured logs, and the (future) transcript
store can see the metadata behind each transition. They do not
replace the ``asyncio.Event`` on the state machine, which handles
the async coordination the TTS cancel path (#71) needs — the event
and the dataclass are the two halves of ADR-0005 Decision 3's
split signal.

All three are ``frozen=True`` so observers can safely retain
references without worrying about mutation after the fact.

Timestamps use ``asyncio.get_event_loop().time()`` (monotonic within
a session) rather than wall-clock time so metrics are robust against
system-clock changes during a session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "BargeInEvent",
    "EndOfTurnEvent",
    "StartOfTurnEvent",
]


@dataclass(frozen=True)
class BargeInEvent:
    """``SPEAKING -> BARGE_IN`` observer payload (ADR-0005 Decision 3).

    Attributes:
        at: Monotonic timestamp when the confirmed-barge-in signal
            fired (seconds; typically ``loop.time()``).
        source: Signal source. ``"flux"`` for Flux ``StartOfTurn``
            confirmation (the only source in Tier 4a). ``"vad"`` and
            ``"force"`` are reserved for later tiers (voice-activity
            detector-driven or explicit application cancel).
        confidence: Signal confidence in [0.0, 1.0] if the source
            provides one (Flux does; VAD may). ``None`` when the
            source does not expose a confidence (``"force"``).
    """

    at: float
    source: Literal["vad", "flux", "force"]
    confidence: float | None = None


@dataclass(frozen=True)
class StartOfTurnEvent:
    """Flux ``StartOfTurn`` observed, pre-confirmation.

    Carried through the detector's internal state but not emitted to
    observers directly in Tier 4a — the SPEAKING -> BARGE_IN
    transition fires with a :class:`BargeInEvent` payload once
    confirmed. Kept here as the natural shape in case a future tier
    exposes pre-confirmation StartOfTurn to observers (e.g. for a
    "detecting speech…" HUD indicator).

    Attributes:
        at: Monotonic timestamp.
        turn_index: Flux's turn index, if present on the wire message.
        confidence: Flux's end-of-turn confidence from the event
            (may be ``None`` on wire; the ``StartOfTurn`` event
            carries ``end_of_turn_confidence`` as a numeric field,
            but tier 4a does not rely on the value — the ``source``
            tag is enough for metrics).
    """

    at: float
    turn_index: int | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class EndOfTurnEvent:
    """Flux ``EndOfTurn`` observed. One payload shape for every
    trigger — observers switch on ``trigger`` if they need to
    differentiate.

    Attributes:
        at: Monotonic timestamp.
        trigger: Flux terminal trigger. ``"manual"`` = response to
            our ForceEndTurn. ``"model"`` = natural end-of-turn
            (post-release under ADR-0005 Decision 1). ``"timeout"``
            = ``eot_timeout_ms`` fired. Values match
            :mod:`agent.stt`'s existing ``_handle_end_of_turn``.
        transcript: Transcript text from the event (may be empty).
        turn_index: Flux's turn index if present on the wire message.
    """

    at: float
    trigger: Literal["manual", "model", "timeout"]
    transcript: str
    turn_index: int | None = None
