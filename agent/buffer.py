"""Short-term conversation buffer (ADR-0006).

Bounded in-memory ring of recent conversation turns. Backs the
text and voice loops' "recent context" view and feeds Anthropic's
message list on each turn. Separate from the long-term SQLite
memory store (`agent/memory.py`, #76+) which handles durable
facts.

Scope boundaries from ADR-0006:

* Eviction is FIFO on turn count. Token-aware pruning (soft cap
  of ~4,000 tokens by default) is the context-assembly layer's
  job per ADR-0007, not this buffer's.
* Persistence to ``session.json`` is #75, not this issue.
* Both ``voice_loop`` and ``text_loop`` append; context
  assembly reads. #74 ships the module; wiring into the loops
  is a later issue.

Not thread-safe. FRIDAY's asyncio-single-threaded model does not
require locking; adding threading support would be a breaking
change tracked at that point.

Public surface:
    Turn — frozen dataclass (role, content, timestamp).
    ConversationBuffer — bounded FIFO ring exposing
        ``append``, ``as_messages``, ``clear``, ``token_count``,
        ``__len__``, and ``__iter__``.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Literal

# -----------------------------------------------------------------
# Constants (ADR-0006)
# -----------------------------------------------------------------

#: Default buffer ceiling per ADR-0006 §Short-term. Revisit after
#: real use once Tier 4 wiring + the context-assembly layer land.
_BUFFER_TURN_LIMIT: Final[int] = 20  # TODO(#74-tuning)

#: Roles accepted by :class:`Turn`. Anthropic Messages API uses
#: these two; "system" is a separate top-level field, not a role.
_ROLES: Final[frozenset[str]] = frozenset({"user", "assistant"})


# -----------------------------------------------------------------
# Token counting
# -----------------------------------------------------------------


def _count_tokens(text: str) -> int:
    """Rough token count for ``text``.

    Simple character-based estimate (4 chars/token) as a
    placeholder — this is the standard English-text approximation
    that is wrong for code, non-English, and anything with many
    rare tokens, but it is right enough for ``ConversationBuffer``'s
    inspection use cases pre-context-assembly.

    ADR-0007 (#79) replaces this with the Anthropic token-counting
    API (with a tiktoken fallback) when context assembly lands. At
    that point ``Turn.token_count`` and ``ConversationBuffer.token_count``
    will delegate to the real counter instead of this helper.

    Args:
        text: The text to estimate.

    Returns:
        0 for empty text; otherwise ``max(1, len(text) // 4)``.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


# -----------------------------------------------------------------
# Turn
# -----------------------------------------------------------------


@dataclass(frozen=True)
class Turn:
    """One side of one conversational exchange.

    Frozen because turns, once appended, do not change — the
    buffer owns ordering and eviction, not per-turn edits.
    Timestamp defaults to ``datetime.now(UTC)`` so most callers
    can skip it; the field is explicit so persistence (#75) and
    tests can reconstruct exact-time turns.

    Validation (``__post_init__``) rejects any role outside
    ``{"user", "assistant"}`` with a ``ValueError`` whose message
    names the invalid value. Loops write to this at per-turn
    frequency, so one typo would silently poison the buffer
    without the runtime check.

    Attributes:
        role: Either ``"user"`` or ``"assistant"``.
        content: The turn's text. May be empty.
        timestamp: UTC-aware ``datetime``; defaults to the current
            time at construction. Must be timezone-aware.
    """

    role: Literal["user", "assistant"]
    content: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.role not in _ROLES:
            raise ValueError(f"Turn.role must be one of {sorted(_ROLES)}; got {self.role!r}")

    def to_message(self) -> dict[str, str]:
        """Return the Anthropic Messages API representation.

        Shape: ``{"role": role, "content": content}``. The
        timestamp is intentionally NOT included — the API doesn't
        accept it and context assembly (#79) reads it directly
        from the ``Turn`` object when it needs recency weighting.
        """
        return {"role": self.role, "content": self.content}

    def token_count(self) -> int:
        """Rough token count for this turn's content (see
        :func:`_count_tokens`)."""
        return _count_tokens(self.content)


# -----------------------------------------------------------------
# ConversationBuffer
# -----------------------------------------------------------------


class ConversationBuffer:
    """Bounded FIFO ring of recent conversation turns.

    Backed by :class:`collections.deque` with ``maxlen=turn_limit``,
    which gives O(1) append and automatic eviction of the oldest
    turn when the ring is full. On overflow, no exception is
    raised; the eviction is silent-by-design — the ring is sized
    to be the "recent context" window, not an audit log.

    Not thread-safe. Single-threaded asyncio usage is assumed.
    """

    def __init__(self, turn_limit: int = _BUFFER_TURN_LIMIT) -> None:
        """Create an empty buffer with the given turn ceiling.

        Args:
            turn_limit: Maximum turns to retain before FIFO
                eviction. Must be ≥ 1 — a limit of 0 would evict
                every turn immediately on append and is almost
                certainly a bug at the call site.

        Raises:
            ValueError: If ``turn_limit`` < 1.
        """
        if turn_limit < 1:
            raise ValueError(f"turn_limit must be ≥ 1; got {turn_limit}")
        self._turns: deque[Turn] = deque(maxlen=turn_limit)
        self._turn_limit: Final[int] = turn_limit

    @property
    def turn_limit(self) -> int:
        """Maximum turns retained before FIFO eviction."""
        return self._turn_limit

    def append(self, turn: Turn) -> None:
        """Append ``turn``, evicting the oldest if the ring is full.

        Args:
            turn: The turn to append. Must already be a valid
                :class:`Turn` — role validation happens at
                construction, not here.
        """
        self._turns.append(turn)

    def as_messages(self) -> list[dict[str, str]]:
        """Return the buffered turns in Anthropic message format.

        The returned list is a fresh snapshot; mutating it does
        NOT affect the buffer's internal state. Callers that want
        to pass this to ``anthropic.messages.create`` can do so
        directly; callers that want to inspect without mutation
        get the same list.
        """
        return [t.to_message() for t in self._turns]

    def clear(self) -> None:
        """Remove all turns. Idempotent on an already-empty buffer."""
        self._turns.clear()

    def token_count(self) -> int:
        """Total approximate token count across all buffered turns.

        Delegates to :meth:`Turn.token_count` on each turn; see
        :func:`_count_tokens` for the estimation note and the
        ADR-0007 (#79) replacement path.
        """
        return sum(t.token_count() for t in self._turns)

    def __len__(self) -> int:
        """Number of turns currently buffered (0 ≤ len ≤ turn_limit)."""
        return len(self._turns)

    def __iter__(self) -> Iterator[Turn]:
        """Iterate turns in oldest-to-newest order."""
        return iter(self._turns)
