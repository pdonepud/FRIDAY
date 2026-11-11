"""Tests for :mod:`agent.context` — context assembly with token budget (ADR-0007)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from agent.buffer import ConversationBuffer, Turn
from agent.context import (
    _BUFFER_PRUNE_TARGET,
    _CONTEXT_BUDGET_TOKENS,
    _MEMORY_SECTION_HEADER,
    _ROLE_OVERHEAD_TOKENS,
    _count_tokens,
    build_messages,
)
from agent.memory import Memory, add_memory, bootstrap_db

# -----------------------------------------------------------------
# Fixture helpers
# -----------------------------------------------------------------


_NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def _fake_memory(mid: int, content: str, *, kind: str = "fact") -> Memory:
    """Build a Memory dataclass with fixed-time fields."""
    return Memory(
        id=mid,
        created_at=_NOW,
        updated_at=_NOW,
        kind=kind,
        content=content,
        metadata={},
    )


def _buffer_with(turns: list[tuple[str, str]], *, turn_limit: int = 20) -> ConversationBuffer:
    """Build a ConversationBuffer from a list of (role, content) pairs."""
    buf = ConversationBuffer(turn_limit=turn_limit)
    for role, content in turns:
        buf.append(Turn(role=role, content=content, timestamp=_NOW))
    return buf


def _install_fake_search(monkeypatch, result):
    """Replace agent.context.search_memories with an async no-op returning ``result``."""

    async def fake(db_path, query, k=5):
        if isinstance(result, BaseException):
            raise result
        return list(result)

    monkeypatch.setattr("agent.context.search_memories", fake)


# =================================================================
# #79 — Under-budget / empty inputs
# =================================================================


async def test_build_messages_under_budget_includes_all_components(monkeypatch):
    """Small inputs on every tier — assembly is a straight concat with
    the memory section appended to base_system and the current user
    turn appended after the prior buffer turns."""
    _install_fake_search(
        monkeypatch,
        [_fake_memory(1, "likes oat milk"), _fake_memory(2, "uses pyenv")],
    )

    buf = _buffer_with([("user", "earlier"), ("assistant", "ok"), ("user", "current turn")])

    system, messages = await build_messages("current turn", buf, "You are FRIDAY.")

    assert system.startswith("You are FRIDAY.")
    assert _MEMORY_SECTION_HEADER in system
    assert "- likes oat milk" in system
    assert "- uses pyenv" in system
    # Prior buffer turns (chronological, excluding the current user Turn)
    # + reconstructed current user Turn as the last message.
    assert messages == [
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "current turn"},
    ]


async def test_build_messages_empty_memory_results_omits_section(monkeypatch):
    """Zero memories → assembled_system == base_system verbatim (no
    header, no blank separator) so Claude doesn't see a dangling
    colon."""
    _install_fake_search(monkeypatch, [])

    buf = _buffer_with([("user", "hi")])
    system, _ = await build_messages("hi", buf, "You are FRIDAY.")
    assert system == "You are FRIDAY."
    assert _MEMORY_SECTION_HEADER not in system


async def test_build_messages_empty_buffer_works(monkeypatch):
    """A buffer with only the current user Turn → messages contains
    just that Turn."""
    _install_fake_search(monkeypatch, [])

    buf = _buffer_with([("user", "only turn")])
    system, messages = await build_messages("only turn", buf, "sys")
    assert messages == [{"role": "user", "content": "only turn"}]
    assert system == "sys"


# =================================================================
# #79 — Memory pruning (bm25 order, lowest-rank dropped first)
# =================================================================


async def test_build_messages_over_budget_on_memories_drops_lowest_ranked_first(
    monkeypatch,
):
    """Memories arrive bm25-ranked (highest first). When the memory
    section doesn't fit, the TAIL is dropped, keeping the highest-
    ranked kept-first."""
    # 10 memories, each padded with 10_000 "y" chars → ~1250 tokens
    # per memory under tiktoken's cl100k_base. Ten of them = ~12_500
    # tokens — well over the ~6400-token prune target, so the tail
    # must drop.
    memories = [_fake_memory(i, f"marker-m{i} " + "y" * 10_000) for i in range(1, 11)]
    _install_fake_search(monkeypatch, memories)

    buf = _buffer_with([("user", "hi")])
    system, _ = await build_messages("hi", buf, "sys")

    # Highest-ranked (index 0 of the list, memory id 1) is kept.
    assert "marker-m1" in system
    # The lowest-ranked tail (id 10) must have dropped.
    assert "marker-m10" not in system


# =================================================================
# #79 — Buffer pruning (chronological, oldest dropped first)
# =================================================================


async def test_build_messages_over_budget_on_buffer_drops_oldest_first(monkeypatch):
    """Oldest Turns drop first; retained Turns stay chronological."""
    _install_fake_search(monkeypatch, [])

    # 50 user/assistant pairs of ~200 chars each → ~50 tokens per turn.
    pairs = []
    for i in range(50):
        pairs.append(("user", f"u{i}: " + "x" * 200))
        pairs.append(("assistant", f"a{i}: " + "y" * 200))
    pairs.append(("user", "now"))

    buf = _buffer_with(pairs, turn_limit=120)

    _, messages = await build_messages("now", buf, "sys")

    # Last message is the current user turn.
    assert messages[-1] == {"role": "user", "content": "now"}
    # Something got dropped off the front.
    prior_contents = [m["content"] for m in messages[:-1]]
    assert "u0:" not in "".join(prior_contents), "oldest turn should have been dropped"
    # And the newest prior turn (a49) should still be there.
    assert any("a49" in c for c in prior_contents)


# =================================================================
# #79 — Catastrophic user turn
# =================================================================


async def test_build_messages_catastrophic_user_turn_warns_and_sends_anyway(monkeypatch, caplog):
    """User turn alone > budget → WARNING, memory section empty,
    prior buffer turns empty, user turn still sent as final message."""
    caplog.set_level(logging.WARNING, logger="agent.context")

    _install_fake_search(monkeypatch, [_fake_memory(1, "ignored memory")])
    # tiktoken compresses repeated chars (64_000 "x" ≈ exactly 8000
    # tokens — right at the boundary). 100_000 chars gives ~12_500
    # tokens, comfortably over the 8000-token budget.
    huge = "x" * 100_000
    buf = _buffer_with([("user", "earlier"), ("assistant", "ok"), ("user", huge)])

    system, messages = await build_messages(huge, buf, "sys")

    assert any("exceeds budget" in rec.message for rec in caplog.records)
    assert _MEMORY_SECTION_HEADER not in system
    assert messages == [{"role": "user", "content": huge}]


# =================================================================
# #79 — Independent 4000-token buffer cap
# =================================================================


async def test_build_messages_buffer_cap_binds_before_overall_cap(monkeypatch):
    """Even when the overall 6400-token budget would admit more buffer,
    the 3200-token buffer prune target bounds the kept prior turns
    independently (ADR-0006 §Short-term + ADR-0007 §Assembly item 4)."""
    _install_fake_search(monkeypatch, [])

    # Fill prior turns so their total >> 3200 tokens but overall
    # budget (6400) still has lots of room.
    pairs = []
    for i in range(40):
        pairs.append(("user", f"u{i}: " + "a" * 400))
        pairs.append(("assistant", f"a{i}: " + "b" * 400))
    pairs.append(("user", "now"))
    buf = _buffer_with(pairs, turn_limit=100)

    _, messages = await build_messages("now", buf, "tiny system")

    # Kept buffer cost must respect the buffer prune target.
    kept_prior_cost = sum(
        _count_tokens(m["content"]) + _ROLE_OVERHEAD_TOKENS for m in messages[:-1]
    )
    assert kept_prior_cost <= _BUFFER_PRUNE_TARGET, (
        f"kept_prior_cost={kept_prior_cost} exceeded buffer prune target {_BUFFER_PRUNE_TARGET}"
    )


async def test_build_messages_both_caps_bind_simultaneously(monkeypatch, caplog):
    """Both caps constraining the same assembly: `cause=` lists both
    names joined alphabetically with `+` (C1). The possible values
    are buffer_cap, overall, user_turn_overflow."""
    caplog.set_level(logging.INFO, logger="agent.context")

    # Many large memories + many large buffer turns → both groups
    # need pruning. Using ~5000-char padding per candidate (~625
    # tokens each under tiktoken) so pre-prune sums comfortably
    # exceed both the overall prune target (6400) and the buffer
    # prune target (3200).
    memories = [_fake_memory(i, f"mem{i} " + "y" * 5_000) for i in range(1, 11)]
    _install_fake_search(monkeypatch, memories)

    pairs = []
    for i in range(20):
        pairs.append(("user", f"u{i}: " + "a" * 5_000))
        pairs.append(("assistant", f"a{i}: " + "b" * 5_000))
    pairs.append(("user", "now"))
    buf = _buffer_with(pairs, turn_limit=60)

    await build_messages("now", buf, "sys")

    # Find the context_pruned INFO line and assert alphabetical join.
    pruned = [r for r in caplog.records if "event=context_pruned" in r.message]
    assert pruned, "expected event=context_pruned log line"
    msg = pruned[0].message
    # Alphabetical: buffer_cap before overall.
    assert "cause=buffer_cap+overall" in msg, f"cause field not alphabetically joined: {msg}"


# =================================================================
# #79 — Error paths: search_memories raises
# =================================================================


async def test_build_messages_search_memories_raises_proceeds_with_zero_memories_and_warns(
    monkeypatch, caplog
):
    """A memory-retrieval exception is swallowed with a WARNING;
    assembly continues with zero memories — read-path parity with
    ADR-0006 §Writer's 'failures log and do not surface'."""
    caplog.set_level(logging.WARNING, logger="agent.context")

    _install_fake_search(monkeypatch, aiosqlite.Error("simulated DB error"))

    buf = _buffer_with([("user", "hi")])
    system, messages = await build_messages("hi", buf, "sys")

    assert _MEMORY_SECTION_HEADER not in system
    assert messages == [{"role": "user", "content": "hi"}]
    assert any("memory retrieval failed" in rec.message for rec in caplog.records)


# =================================================================
# #79 — Telemetry (log line asserted via caplog)
# =================================================================


async def test_build_messages_pruning_telemetry_fires_when_anything_cut(monkeypatch, caplog):
    """Any pruning emits an INFO ``event=context_pruned`` with the
    documented key set."""
    caplog.set_level(logging.INFO, logger="agent.context")

    # Ten large memories (~1250 tokens each) → total ~12_500 ≫
    # the memory budget, so pruning must fire.
    memories = [_fake_memory(i, f"m{i} " + "y" * 10_000) for i in range(1, 11)]
    _install_fake_search(monkeypatch, memories)

    buf = _buffer_with([("user", "hi")])
    await build_messages("hi", buf, "sys")

    pruned = [r for r in caplog.records if "event=context_pruned" in r.message]
    assert pruned
    msg = pruned[0].message
    for key in (
        "memories_in=",
        "memories_kept=",
        "buffer_turns_in=",
        "buffer_turns_kept=",
        "estimated_tokens=",
        "budget=",
        "prune_target=",
        "cause=",
    ):
        assert key in msg, f"missing {key!r} in telemetry: {msg}"


async def test_build_messages_pruning_telemetry_does_not_fire_under_budget(monkeypatch, caplog):
    """Negative control: nothing cut → no `event=context_pruned` line."""
    caplog.set_level(logging.INFO, logger="agent.context")

    _install_fake_search(monkeypatch, [_fake_memory(1, "small")])
    buf = _buffer_with([("user", "hi")])

    await build_messages("hi", buf, "sys")

    assert not any("event=context_pruned" in rec.message for rec in caplog.records)


# =================================================================
# #79 — Memory section format
# =================================================================


async def test_build_messages_memory_section_format_matches_adr(monkeypatch):
    """Exact header string + one bullet per memory + content only
    (no kind / timestamps)."""
    _install_fake_search(
        monkeypatch,
        [
            _fake_memory(1, "likes oat milk"),
            _fake_memory(2, "uses pyenv"),
            _fake_memory(3, "owns a cat named Mittens"),
        ],
    )

    buf = _buffer_with([("user", "hi")])
    system, _ = await build_messages("hi", buf, "You are FRIDAY.")

    expected_section = (
        f"{_MEMORY_SECTION_HEADER}\n- likes oat milk\n- uses pyenv\n- owns a cat named Mittens"
    )
    assert expected_section in system
    # No kind or timestamp leaked.
    assert "fact" not in system.lower().split("\n\n")[1]
    assert "2026-11-10" not in system


# =================================================================
# #79 — Return shape + role-first invariant
# =================================================================


async def test_build_messages_return_shape(monkeypatch):
    """Returns ``(str, list[dict])``; messages[-1] is the current user
    turn verbatim; messages[0]["role"] is 'user' under normal
    role-alternating input."""
    _install_fake_search(monkeypatch, [])

    buf = _buffer_with([("user", "earlier"), ("assistant", "ok"), ("user", "now")])
    system, messages = await build_messages("now", buf, "sys")

    assert isinstance(system, str)
    assert isinstance(messages, list)
    for m in messages:
        assert isinstance(m, dict)
        assert set(m.keys()) == {"role", "content"}
    assert messages[-1] == {"role": "user", "content": "now"}
    assert messages[0]["role"] == "user"


async def test_build_messages_messages_zero_is_user_after_prefix_pruning(monkeypatch):
    """After pruning, if the retained prior-turn slice would start
    with an assistant (the FIFO-overflow corner from #101 CR R2),
    the assembler strips leading non-user Turns so Anthropic's
    messages[0].role=user invariant holds."""
    _install_fake_search(monkeypatch, [])

    # Prior history starts with an assistant Turn (a possible FIFO
    # overflow shape).
    buf = _buffer_with(
        [
            ("assistant", "stranded leading"),
            ("user", "u1"),
            ("assistant", "a1"),
            ("user", "now"),
        ]
    )

    _, messages = await build_messages("now", buf, "sys")
    assert messages[0]["role"] == "user"
    # "stranded leading" should NOT appear in any message.
    for m in messages:
        assert "stranded leading" not in m["content"]


async def test_build_messages_chronological_ordering_preserved(monkeypatch):
    """Retained prior turns appear in oldest-to-newest order; current
    user turn is last."""
    _install_fake_search(monkeypatch, [])

    buf = _buffer_with(
        [
            ("user", "one"),
            ("assistant", "two"),
            ("user", "three"),
            ("assistant", "four"),
            ("user", "now"),
        ]
    )

    _, messages = await build_messages("now", buf, "sys")
    assert [m["content"] for m in messages] == [
        "one",
        "two",
        "three",
        "four",
        "now",
    ]


# =================================================================
# #79 — tiktoken fallback + budget drift
# =================================================================


async def test_build_messages_tiktoken_unavailable_falls_back_to_char_four_estimation_and_warns(
    monkeypatch, caplog
):
    """Encoder init failure → char/4 estimation + WARNING."""
    import agent.context

    caplog.set_level(logging.WARNING, logger="agent.context")

    # Force _get_encoder to behave as if tiktoken were unavailable.
    monkeypatch.setattr(agent.context, "_encoder", None, raising=False)
    monkeypatch.setattr(agent.context, "_encoder_available", True, raising=False)

    def boom(name):
        raise RuntimeError("simulated network failure")

    monkeypatch.setattr("agent.context.tiktoken.get_encoding", boom)

    # _count_tokens should fall back to char/4 and log once.
    assert _count_tokens("hello there") == max(1, len("hello there") // 4)

    assert any("tiktoken unavailable" in rec.message for rec in caplog.records)

    # A second call should NOT re-log (idempotent WARNING).
    caplog.clear()
    assert _count_tokens("another string") > 0
    assert not any("tiktoken unavailable" in rec.message for rec in caplog.records)


async def test_build_messages_budget_estimate_drift_warning_fires_when_verification_exceeds_budget(
    monkeypatch, caplog
):
    """Rare drift case: per-candidate estimates stay under budget but
    final verification exceeds it → ``event=budget_estimate_drift``
    WARNING, assembly sent unchanged (no re-pruning)."""
    import agent.context

    caplog.set_level(logging.WARNING, logger="agent.context")

    _install_fake_search(monkeypatch, [_fake_memory(1, "small memory")])

    # Monkeypatch _count_tokens so pruning sees everything as cheap,
    # but the final verification pass over-reports.
    real_count = agent.context._count_tokens

    call_state = {"count": 0}

    def stubbed(text: str) -> int:
        call_state["count"] += 1
        # The final verification is the burst of calls after the
        # pruning phase; with a tiny input (1 memory, 1 user turn)
        # there are only a handful of pre-prune counts. After ~6
        # calls, start over-reporting hugely.
        if call_state["count"] > 6:
            return _CONTEXT_BUDGET_TOKENS + 10_000
        return real_count(text)

    monkeypatch.setattr(agent.context, "_count_tokens", stubbed)

    buf = _buffer_with([("user", "hi")])
    await build_messages("hi", buf, "sys")

    assert any("event=budget_estimate_drift" in rec.message for rec in caplog.records)


# =================================================================
# #79 — Real-DB integration smoke
# =================================================================


@pytest.fixture
async def bootstrapped_memory_db(tmp_path: Path) -> Path:
    """Bootstrapped memory.db at tmp_path, pre-seeded with 3 facts."""
    path = tmp_path / "memory.db"
    await bootstrap_db(path)
    await add_memory(path, kind="fact", content="FRIDAY uses SQLite for memory")
    await add_memory(path, kind="fact", content="FRIDAY uses FTS5 for keyword search")
    await add_memory(path, kind="preference", content="prefers oat milk")
    return path


async def test_build_messages_integration_with_real_search_memories(
    monkeypatch, bootstrapped_memory_db
):
    """End-to-end smoke: the real FTS5 search_memories feeds
    build_messages and produces a non-empty memory section."""
    monkeypatch.setattr(
        "agent.context.default_memory_db_path",
        lambda: bootstrapped_memory_db,
    )

    # Query uses a single term present in two seeded memories so
    # implicit-AND MATCH finds them deterministically. (Longer natural
    # queries would normalize to several terms, each of which must
    # appear in the row — the test here is the integration smoke, not
    # a retrieval-quality probe.)
    buf = _buffer_with([("user", "FRIDAY")])
    system, messages = await build_messages("FRIDAY", buf, "You are FRIDAY.")

    assert _MEMORY_SECTION_HEADER in system
    # At least one seeded content should land in the section.
    assert ("FRIDAY uses SQLite" in system) or ("FRIDAY uses FTS5" in system)
    assert messages[-1]["content"] == "FRIDAY"


# =================================================================
# #79 CR F1 — tiktoken special-token-literal handling
# =================================================================


_SPECIAL = "<|endoftext|>"


@pytest.mark.parametrize("vector", ["base_system", "current_turn", "buffer_turn", "memory"])
async def test_build_messages_counts_special_token_literals_as_plain_text(monkeypatch, vector):
    """CR F1: ``tiktoken.Encoding.encode`` defaults to
    ``disallowed_special='all'`` and raises ValueError on literal
    special-token strings. Every input to build_messages flows through
    ``_count_tokens``; a user who types any such string would brick
    text_loop (user Turn already appended + writer already fired;
    ValueError escapes Phase 5, Turn poisoned into session.json, next
    load re-crashes). Fixed by switching ``_count_tokens`` to
    ``encode_ordinary``, which treats input as plain bytes.

    This parametrized test covers all four input vectors — base_system,
    current_turn, a buffer Turn, and a returned Memory — each asserts
    ``build_messages`` returns without raising AND the literal is
    preserved verbatim in whichever slot originated it (no
    stripping / re-encoding on the way through)."""
    base_system = "You are FRIDAY."
    current_turn = "hello"
    buffer_turns = [("user", "earlier"), ("assistant", "ok"), ("user", current_turn)]
    memories: list = []

    if vector == "base_system":
        base_system = f"You are FRIDAY. {_SPECIAL}"
    elif vector == "current_turn":
        current_turn = f"hello {_SPECIAL} world"
        buffer_turns[-1] = ("user", current_turn)
    elif vector == "buffer_turn":
        buffer_turns[0] = ("user", f"earlier with {_SPECIAL} inside")
    elif vector == "memory":
        memories = [_fake_memory(1, f"a memory containing {_SPECIAL} verbatim")]

    _install_fake_search(monkeypatch, memories)
    buf = _buffer_with(buffer_turns)

    # Primary assertion: does not raise.
    system, messages = await build_messages(current_turn, buf, base_system)

    # Return shape sanity.
    assert isinstance(system, str)
    assert isinstance(messages, list)
    assert messages[-1] == {"role": "user", "content": current_turn}

    # Literal preserved verbatim in its originating slot.
    if vector == "base_system":
        assert _SPECIAL in system
    elif vector == "current_turn":
        assert _SPECIAL in messages[-1]["content"]
    elif vector == "buffer_turn":
        # The oldest prior Turn should still contain the literal
        # (small buffer, nothing dropped).
        assert any(_SPECIAL in m["content"] for m in messages[:-1])
    elif vector == "memory":
        # The memory section should carry the bullet with the literal.
        assert _SPECIAL in system


# =================================================================
# #79 CR F2 — warm_encoder (off-event-loop tiktoken warmup)
# =================================================================


@pytest.fixture
def _reset_encoder_state(monkeypatch):
    """Reset the module-level encoder globals before each warm_encoder
    test. ``agent.context`` carries one-shot state (``_encoder`` /
    ``_encoder_available``) that would otherwise leak across tests."""
    import agent.context

    monkeypatch.setattr(agent.context, "_encoder", None, raising=False)
    monkeypatch.setattr(agent.context, "_encoder_available", True, raising=False)


async def test_warm_encoder_populates_encoder_on_happy_path(_reset_encoder_state, monkeypatch):
    """Happy path: `warm_encoder` populates `_encoder` with whatever
    `tiktoken.get_encoding` returns."""
    import agent.context

    sentinel = object()
    called = []

    def fake_get_encoding(name):
        called.append(name)
        return sentinel

    monkeypatch.setattr("agent.context.tiktoken.get_encoding", fake_get_encoding)

    await agent.context.warm_encoder()

    assert agent.context._encoder is sentinel
    assert agent.context._encoder_available is True
    assert called == ["cl100k_base"]


async def test_warm_encoder_falls_back_on_exception(_reset_encoder_state, monkeypatch, caplog):
    """`tiktoken.get_encoding` raising flips `_encoder_available` to
    False and emits the one-time WARNING, matching the lazy path's
    posture so subsequent `_count_tokens` calls degrade to char/4."""
    import agent.context

    caplog.set_level(logging.WARNING, logger="agent.context")

    def boom(name):
        raise RuntimeError("simulated network failure")

    monkeypatch.setattr("agent.context.tiktoken.get_encoding", boom)

    await agent.context.warm_encoder()

    assert agent.context._encoder is None
    assert agent.context._encoder_available is False
    assert any("tiktoken unavailable" in rec.message for rec in caplog.records)


async def test_warm_encoder_falls_back_on_timeout(_reset_encoder_state, monkeypatch, caplog):
    """A sync download that overruns the warmup timeout surfaces as
    TimeoutError from asyncio.wait_for; warm_encoder catches it,
    flips the fallback flag, and emits a WARNING. The background
    thread is NOT cancelled (asyncio can't cancel a sync to_thread
    worker) — documented in the function's docstring; the thread's
    lingering work is harmless because `_get_encoder` would
    transparently promote a late result."""
    import time

    import agent.context

    caplog.set_level(logging.WARNING, logger="agent.context")

    def slow_get_encoding(name):
        # NOTE: time.sleep, not asyncio.sleep — this runs in the
        # asyncio.to_thread worker where asyncio.sleep is a no-op.
        time.sleep(0.5)
        return object()

    monkeypatch.setattr("agent.context.tiktoken.get_encoding", slow_get_encoding)

    await agent.context.warm_encoder(timeout=0.1)

    assert agent.context._encoder_available is False
    assert any("warmup timed out" in rec.message for rec in caplog.records)


async def test_warm_encoder_is_idempotent_when_already_warm(_reset_encoder_state, monkeypatch):
    """When `_encoder` is already populated, `warm_encoder` returns
    immediately and does NOT call `tiktoken.get_encoding` again."""
    import agent.context

    sentinel = object()
    monkeypatch.setattr(agent.context, "_encoder", sentinel, raising=False)

    called = []

    def tracking(name):
        called.append(name)
        return object()

    monkeypatch.setattr("agent.context.tiktoken.get_encoding", tracking)

    await agent.context.warm_encoder()

    assert agent.context._encoder is sentinel
    assert called == []


async def test_warm_encoder_is_idempotent_when_already_failed(_reset_encoder_state, monkeypatch):
    """When `_encoder_available` is already False (a prior attempt
    failed), `warm_encoder` returns immediately — no retry on
    repeated warmups within a process lifetime."""
    import agent.context

    monkeypatch.setattr(agent.context, "_encoder_available", False, raising=False)

    called = []

    def tracking(name):
        called.append(name)
        return object()

    monkeypatch.setattr("agent.context.tiktoken.get_encoding", tracking)

    await agent.context.warm_encoder()

    assert agent.context._encoder is None
    assert called == []
