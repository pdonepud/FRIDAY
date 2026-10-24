"""Unit tests for :mod:`agent.events` (#72)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from agent.events import BargeInEvent, EndOfTurnEvent, StartOfTurnEvent


def test_barge_in_event_construction() -> None:
    ev = BargeInEvent(at=1.5, source="flux", confidence=0.87)
    assert ev.at == 1.5
    assert ev.source == "flux"
    assert ev.confidence == 0.87


def test_barge_in_event_confidence_defaults_none() -> None:
    ev = BargeInEvent(at=0.0, source="force")
    assert ev.confidence is None


def test_barge_in_event_is_frozen() -> None:
    ev = BargeInEvent(at=0.0, source="flux")
    with pytest.raises(FrozenInstanceError):
        ev.at = 2.0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        ev.source = "vad"  # type: ignore[misc]


def test_start_of_turn_event_construction_and_defaults() -> None:
    ev = StartOfTurnEvent(at=1.0, turn_index=3, confidence=0.9)
    assert ev.at == 1.0
    assert ev.turn_index == 3
    assert ev.confidence == 0.9

    minimal = StartOfTurnEvent(at=2.0)
    assert minimal.turn_index is None
    assert minimal.confidence is None


def test_start_of_turn_event_is_frozen() -> None:
    ev = StartOfTurnEvent(at=0.0)
    with pytest.raises(FrozenInstanceError):
        ev.turn_index = 7  # type: ignore[misc]


def test_end_of_turn_event_construction() -> None:
    ev = EndOfTurnEvent(at=5.0, trigger="manual", transcript="hello", turn_index=2)
    assert ev.at == 5.0
    assert ev.trigger == "manual"
    assert ev.transcript == "hello"
    assert ev.turn_index == 2


def test_end_of_turn_event_turn_index_defaults_none() -> None:
    ev = EndOfTurnEvent(at=0.0, trigger="model", transcript="")
    assert ev.turn_index is None


def test_end_of_turn_event_is_frozen() -> None:
    ev = EndOfTurnEvent(at=0.0, trigger="model", transcript="")
    with pytest.raises(FrozenInstanceError):
        ev.transcript = "mutated"  # type: ignore[misc]


def test_events_are_equal_by_value() -> None:
    """Dataclass equality — observers can diff payloads without identity games."""
    a = BargeInEvent(at=1.0, source="flux", confidence=0.5)
    b = BargeInEvent(at=1.0, source="flux", confidence=0.5)
    c = BargeInEvent(at=1.0, source="flux", confidence=0.6)
    assert a == b
    assert a != c
