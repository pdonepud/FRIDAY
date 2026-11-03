"""Tests for :mod:`agent.buffer` (ADR-0006 §Short-term)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agent.buffer import (
    _BUFFER_TURN_LIMIT,
    ConversationBuffer,
    Turn,
)

# -----------------------------------------------------------------
# Turn construction + defaults
# -----------------------------------------------------------------


def test_turn_defaults_to_utc_now_timestamp():
    """``Turn.timestamp`` defaults to ``datetime.now(UTC)`` and is
    timezone-aware; a window check avoids clock-skew flakiness."""
    before = datetime.now(UTC)
    turn = Turn(role="user", content="hi")
    after = datetime.now(UTC)
    assert turn.timestamp.tzinfo is not None
    assert before <= turn.timestamp <= after


def test_turn_accepts_explicit_timestamp():
    """Explicit timestamp survives construction unchanged."""
    when = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    turn = Turn(role="assistant", content="ok", timestamp=when)
    assert turn.timestamp == when


def test_turn_is_frozen():
    """Attempting to mutate a Turn raises ``FrozenInstanceError``."""
    from dataclasses import FrozenInstanceError

    turn = Turn(role="user", content="hi")
    with pytest.raises(FrozenInstanceError):
        turn.content = "altered"  # type: ignore[misc]


# -----------------------------------------------------------------
# Role validation
# -----------------------------------------------------------------


@pytest.mark.parametrize("bad_role", ["system", "USER", "", "User", "model"])
def test_turn_rejects_invalid_role(bad_role):
    """Any role outside {"user", "assistant"} raises ValueError
    with the invalid value named in the message."""
    with pytest.raises(ValueError, match="Turn.role must be"):
        Turn(role=bad_role, content="hi")  # type: ignore[arg-type]


@pytest.mark.parametrize("good_role", ["user", "assistant"])
def test_turn_accepts_valid_roles(good_role):
    """Both canonical roles construct cleanly."""
    turn = Turn(role=good_role, content="x")
    assert turn.role == good_role


# -----------------------------------------------------------------
# Turn message + token_count
# -----------------------------------------------------------------


def test_turn_to_message_shape():
    """``to_message`` returns Anthropic Messages API shape and
    omits the timestamp (not part of that API)."""
    turn = Turn(role="user", content="hello")
    assert turn.to_message() == {"role": "user", "content": "hello"}


def test_turn_token_count_empty_content():
    """Empty content → 0 tokens."""
    assert Turn(role="user", content="").token_count() == 0


def test_turn_token_count_short_content():
    """Non-empty content → max(1, len // 4) rounded down, never 0."""
    # "hi" → len=2 → max(1, 0) = 1
    assert Turn(role="user", content="hi").token_count() == 1
    # "abcd" → len=4 → max(1, 1) = 1
    assert Turn(role="user", content="abcd").token_count() == 1
    # "a" * 40 → len=40 → max(1, 10) = 10
    assert Turn(role="user", content="a" * 40).token_count() == 10


# -----------------------------------------------------------------
# Buffer construction + guards
# -----------------------------------------------------------------


def test_buffer_default_turn_limit_matches_adr():
    """Default turn_limit comes from the module constant."""
    buf = ConversationBuffer()
    assert buf.turn_limit == _BUFFER_TURN_LIMIT == 20


def test_buffer_accepts_custom_turn_limit():
    buf = ConversationBuffer(turn_limit=5)
    assert buf.turn_limit == 5


@pytest.mark.parametrize("bad_limit", [0, -1, -100])
def test_buffer_rejects_turn_limit_below_one(bad_limit):
    """A limit of 0 or negative is almost certainly a call-site bug —
    the buffer refuses it with ValueError at construction."""
    with pytest.raises(ValueError, match="turn_limit must be"):
        ConversationBuffer(turn_limit=bad_limit)


def test_buffer_accepts_turn_limit_of_one():
    """turn_limit=1 is a legal edge: the buffer holds only the
    most recent turn, evicting on every append after the first."""
    buf = ConversationBuffer(turn_limit=1)
    buf.append(Turn(role="user", content="a"))
    buf.append(Turn(role="assistant", content="b"))
    assert len(buf) == 1
    assert list(buf)[0].content == "b"


# -----------------------------------------------------------------
# Append + len + iter
# -----------------------------------------------------------------


def test_buffer_append_and_len():
    buf = ConversationBuffer(turn_limit=5)
    assert len(buf) == 0
    buf.append(Turn(role="user", content="a"))
    buf.append(Turn(role="assistant", content="b"))
    assert len(buf) == 2


def test_buffer_iter_preserves_insertion_order():
    """__iter__ yields turns oldest-to-newest."""
    buf = ConversationBuffer(turn_limit=5)
    contents = ["a", "b", "c"]
    for i, c in enumerate(contents):
        role = "user" if i % 2 == 0 else "assistant"
        buf.append(Turn(role=role, content=c))
    assert [t.content for t in buf] == contents


# -----------------------------------------------------------------
# FIFO eviction + overflow
# -----------------------------------------------------------------


def test_buffer_fifo_eviction_drops_oldest_on_overflow():
    """Appending past turn_limit silently evicts the oldest turn
    — the ring is bounded, not an audit log."""
    buf = ConversationBuffer(turn_limit=3)
    for i in range(5):
        role = "user" if i % 2 == 0 else "assistant"
        buf.append(Turn(role=role, content=f"turn-{i}"))
    assert len(buf) == 3
    contents = [t.content for t in buf]
    # Oldest two (turn-0, turn-1) evicted; last three retained.
    assert contents == ["turn-2", "turn-3", "turn-4"]


def test_buffer_overflow_is_silent():
    """No exception on append-while-full. The eviction is the
    contract, not an error signal."""
    buf = ConversationBuffer(turn_limit=2)
    buf.append(Turn(role="user", content="a"))
    buf.append(Turn(role="assistant", content="b"))
    # Must not raise.
    buf.append(Turn(role="user", content="c"))
    assert len(buf) == 2


# -----------------------------------------------------------------
# as_messages
# -----------------------------------------------------------------


def test_buffer_as_messages_shape_and_order():
    buf = ConversationBuffer(turn_limit=5)
    buf.append(Turn(role="user", content="hi"))
    buf.append(Turn(role="assistant", content="hello"))
    assert buf.as_messages() == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def test_buffer_as_messages_returns_fresh_snapshot():
    """Mutating the returned list must not affect the buffer —
    callers (and the message list we pass to Anthropic) should
    not be able to poison the ring by accident."""
    buf = ConversationBuffer(turn_limit=5)
    buf.append(Turn(role="user", content="a"))
    snapshot = buf.as_messages()
    snapshot.append({"role": "assistant", "content": "injected"})
    snapshot[0]["content"] = "altered"
    # Buffer is unaffected on both axes.
    assert len(buf) == 1
    assert buf.as_messages() == [{"role": "user", "content": "a"}]


# -----------------------------------------------------------------
# clear
# -----------------------------------------------------------------


def test_buffer_clear_removes_all_turns():
    buf = ConversationBuffer(turn_limit=5)
    buf.append(Turn(role="user", content="a"))
    buf.append(Turn(role="assistant", content="b"))
    buf.clear()
    assert len(buf) == 0
    assert buf.as_messages() == []


def test_buffer_clear_is_idempotent_on_empty():
    """Repeated clear on an empty buffer must not raise."""
    buf = ConversationBuffer(turn_limit=5)
    buf.clear()
    buf.clear()
    assert len(buf) == 0


# -----------------------------------------------------------------
# token_count on buffer
# -----------------------------------------------------------------


def test_buffer_token_count_empty():
    assert ConversationBuffer(turn_limit=5).token_count() == 0


def test_buffer_token_count_sums_turns():
    """Buffer token_count = sum of each turn's token_count."""
    buf = ConversationBuffer(turn_limit=5)
    buf.append(Turn(role="user", content="a" * 40))  # 10 tokens
    buf.append(Turn(role="assistant", content="b" * 20))  # 5 tokens
    assert buf.token_count() == 15


def test_buffer_token_count_updates_on_eviction():
    """When FIFO eviction drops a turn, its tokens leave the sum."""
    buf = ConversationBuffer(turn_limit=2)
    buf.append(Turn(role="user", content="a" * 40))  # 10 tokens
    buf.append(Turn(role="assistant", content="b" * 20))  # 5 tokens
    assert buf.token_count() == 15
    # Append a third → first is evicted, so 10 drops out and new 2 joins.
    buf.append(Turn(role="user", content="c" * 8))  # 2 tokens
    assert buf.token_count() == 7  # 5 + 2
