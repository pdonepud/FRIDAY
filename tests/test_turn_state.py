"""Unit tests for :mod:`agent.turn_state` (#72, ADR-0005 Decision 4).

Covers:

* Programmatic transition matrix — every ``(from, to)`` pair across
  the five :class:`TurnState` values is parametrized once as a legal
  case and once as an illegal case, per the frozenset the module
  encodes.
* Trigger first-class behaviour: canonical trigger set, trigger
  value appears in both the observer args and the illegal-transition
  WARNING log line.
* Observer error isolation: one observer raising does not break
  dispatch to subsequent observers and does not corrupt state.
* Barge-in event lifecycle: set on ``SPEAKING → BARGE_IN``, cleared
  on entry to ``SPEAKING``.

No loop surface area touched here — those tests live in
``tests/test_loop.py`` once the voice-loop refactor lands in a
later commit.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import pytest

from agent.turn_state import (
    _LEGAL_TRANSITIONS,
    TRIGGERS,
    TurnState,
    TurnStateMachine,
)

# ---------------------------------------------------------------------------
# Matrix construction — every (from, to) pair across all five states.
# ---------------------------------------------------------------------------

_ALL_STATES: list[TurnState] = list(TurnState)
_ALL_PAIRS: list[tuple[TurnState, TurnState]] = [(f, t) for f in _ALL_STATES for t in _ALL_STATES]
_LEGAL_PAIRS: list[tuple[TurnState, TurnState]] = sorted(
    _LEGAL_TRANSITIONS, key=lambda p: (p[0].name, p[1].name)
)
_ILLEGAL_PAIRS: list[tuple[TurnState, TurnState]] = sorted(
    (p for p in _ALL_PAIRS if p not in _LEGAL_TRANSITIONS),
    key=lambda p: (p[0].name, p[1].name),
)

# Shortest legal walk from IDLE to each state. Used by matrix tests
# to arrive at the ``from_state`` they want to exercise.
_SHORTEST_PATH_TO: dict[TurnState, list[TurnState]] = {
    TurnState.IDLE: [],
    TurnState.LISTENING: [TurnState.LISTENING],
    TurnState.THINKING: [TurnState.LISTENING, TurnState.THINKING],
    TurnState.SPEAKING: [TurnState.LISTENING, TurnState.THINKING, TurnState.SPEAKING],
    TurnState.BARGE_IN: [
        TurnState.LISTENING,
        TurnState.THINKING,
        TurnState.SPEAKING,
        TurnState.BARGE_IN,
    ],
}


def _walk_to(sm: TurnStateMachine, target: TurnState) -> None:
    """Walk ``sm`` from its current state to ``target`` via the shortest
    legal path. Trigger value is arbitrary — the machine does not
    dispatch on trigger; it only enforces legal ``(from, to)`` pairs.
    """
    for step in _SHORTEST_PATH_TO[target]:
        ok = sm.transition(step, trigger="force")
        assert ok, f"walk-to setup failed at {step.name}; current={sm.current().name}"


# ---------------------------------------------------------------------------
# Legal transitions — state changes, observer fires exactly once
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "from_state, to_state",
    _LEGAL_PAIRS,
    ids=[f"{p[0].name}->{p[1].name}" for p in _LEGAL_PAIRS],
)
def test_legal_transition_changes_state_and_fires_observer(
    from_state: TurnState, to_state: TurnState
) -> None:
    sm = TurnStateMachine()
    _walk_to(sm, from_state)
    assert sm.current() == from_state

    observer = MagicMock(name=f"obs-{from_state.name}-to-{to_state.name}")
    sm.on_change(observer)

    payload = {"from": from_state.name, "to": to_state.name}
    accepted = sm.transition(to_state, trigger="model", payload=payload)

    assert accepted is True
    assert sm.current() == to_state
    observer.assert_called_once_with(from_state, to_state, "model", payload)


@pytest.mark.parametrize("trigger", TRIGGERS)
def test_each_canonical_trigger_passes_through_to_observer(trigger: str) -> None:
    sm = TurnStateMachine()
    observer = MagicMock()
    sm.on_change(observer)
    accepted = sm.transition(TurnState.LISTENING, trigger=trigger)  # type: ignore[arg-type]

    assert accepted is True
    observer.assert_called_once_with(TurnState.IDLE, TurnState.LISTENING, trigger, None)


# ---------------------------------------------------------------------------
# Illegal transitions — WARNING logged, state unchanged, no observer fire
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "from_state, to_state",
    _ILLEGAL_PAIRS,
    ids=[f"{p[0].name}->{p[1].name}" for p in _ILLEGAL_PAIRS],
)
def test_illegal_transition_rejected_warned_and_no_observer_fire(
    from_state: TurnState,
    to_state: TurnState,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sm = TurnStateMachine()
    _walk_to(sm, from_state)
    assert sm.current() == from_state

    observer = MagicMock(name=f"obs-illegal-{from_state.name}-to-{to_state.name}")
    sm.on_change(observer)

    with caplog.at_level(logging.WARNING, logger="agent.turn_state"):
        accepted = sm.transition(to_state, trigger="manual")

    assert accepted is False, f"illegal transition {from_state.name}->{to_state.name} was accepted"
    assert sm.current() == from_state, (
        f"state changed on illegal transition {from_state.name}->{to_state.name}: "
        f"now {sm.current().name}"
    )
    observer.assert_not_called()

    reject_msgs = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "illegal transition rejected" in r.getMessage()
    ]
    assert reject_msgs, (
        f"expected WARNING for illegal {from_state.name}->{to_state.name}; "
        f"records={[r.getMessage() for r in caplog.records]}"
    )
    msg = reject_msgs[0]
    assert from_state.name in msg
    assert to_state.name in msg
    assert "manual" in msg


# ---------------------------------------------------------------------------
# Observer error isolation
# ---------------------------------------------------------------------------


def test_observer_that_raises_does_not_block_subsequent_observers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _boomer(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    sm = TurnStateMachine()
    good = MagicMock(name="good")
    sm.on_change(_boomer)
    sm.on_change(good)

    with caplog.at_level(logging.WARNING, logger="agent.turn_state"):
        accepted = sm.transition(TurnState.LISTENING, trigger="model")

    assert accepted is True
    assert sm.current() == TurnState.LISTENING
    good.assert_called_once_with(TurnState.IDLE, TurnState.LISTENING, "model", None)

    err_logs = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "raised" in r.getMessage()
    ]
    assert err_logs, "expected WARNING naming the raising observer"
    assert "RuntimeError" in err_logs[0] or "boom" in err_logs[0]


def test_observer_that_raises_does_not_corrupt_state() -> None:
    """A second transition after a raising observer still works end-to-end."""
    sm = TurnStateMachine()
    sm.on_change(lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("boom")))

    assert sm.transition(TurnState.LISTENING, trigger="model") is True
    assert sm.current() == TurnState.LISTENING
    assert sm.transition(TurnState.THINKING, trigger="model") is True
    assert sm.current() == TurnState.THINKING


# ---------------------------------------------------------------------------
# Barge-in event lifecycle
# ---------------------------------------------------------------------------


def test_initial_state_is_idle_and_barge_in_event_unset() -> None:
    sm = TurnStateMachine()
    assert sm.current() == TurnState.IDLE
    assert isinstance(sm.barge_in_event, asyncio.Event)
    assert not sm.barge_in_event.is_set()


def test_barge_in_event_set_on_speaking_to_barge_in() -> None:
    sm = TurnStateMachine()
    _walk_to(sm, TurnState.SPEAKING)
    assert not sm.barge_in_event.is_set()

    assert sm.transition(TurnState.BARGE_IN, trigger="flux") is True
    assert sm.barge_in_event.is_set()


def test_barge_in_event_cleared_on_fresh_speaking_entry() -> None:
    sm = TurnStateMachine()
    _walk_to(sm, TurnState.SPEAKING)
    sm.transition(TurnState.BARGE_IN, trigger="flux")
    assert sm.barge_in_event.is_set()

    # Return to LISTENING, then advance to a fresh SPEAKING: event
    # must be cleared so the next SPEAKING period starts with a
    # clean signal slot.
    sm.transition(TurnState.LISTENING, trigger="manual")
    sm.transition(TurnState.THINKING, trigger="model")
    sm.transition(TurnState.SPEAKING, trigger="model")
    assert not sm.barge_in_event.is_set()


def test_barge_in_event_not_set_on_other_transitions() -> None:
    """Only ``SPEAKING -> BARGE_IN`` sets the event; e.g. a clean
    ``SPEAKING -> LISTENING`` end leaves it unset."""
    sm = TurnStateMachine()
    _walk_to(sm, TurnState.SPEAKING)
    sm.transition(TurnState.LISTENING, trigger="manual")
    assert not sm.barge_in_event.is_set()


def test_speaking_entry_clears_barge_in_event_unconditionally() -> None:
    """The ``to == SPEAKING`` branch inside ``transition()`` MUST clear
    the event regardless of how it got set — e.g. a stale signal from
    outside the state-machine proper (test fixture, future code path,
    accidental external ``set()``). This guards against a regression
    that would leave the TTS cancel path (#71) seeing a pre-fired
    signal on fresh SPEAKING entry.
    """
    sm = TurnStateMachine()
    _walk_to(sm, TurnState.THINKING)
    sm.barge_in_event.set()  # simulate a stale/foreign set
    assert sm.barge_in_event.is_set()
    assert sm.transition(TurnState.SPEAKING, trigger="model") is True
    assert not sm.barge_in_event.is_set()


# ---------------------------------------------------------------------------
# Payload default
# ---------------------------------------------------------------------------


def test_transition_default_payload_is_none() -> None:
    sm = TurnStateMachine()
    observer = MagicMock()
    sm.on_change(observer)
    sm.transition(TurnState.LISTENING, trigger="model")
    observer.assert_called_once_with(TurnState.IDLE, TurnState.LISTENING, "model", None)


# ---------------------------------------------------------------------------
# Sanity checks on the encoded transition set
# ---------------------------------------------------------------------------


def test_legal_transition_set_has_eight_edges() -> None:
    """The task spec enumerates exactly eight legal transitions. If this
    count changes, either the spec moved or someone added an edge
    without updating the ADR / issue — this test is the tripwire."""
    assert len(_LEGAL_TRANSITIONS) == 8


def test_no_self_transitions_are_legal() -> None:
    """Including ``BARGE_IN -> BARGE_IN`` on a re-emitted signal — the
    task spec's "Likely CR targets" calls that out explicitly."""
    for s in _ALL_STATES:
        assert (s, s) not in _LEGAL_TRANSITIONS, (
            f"self-transition {s.name}->{s.name} is in the legal set"
        )
