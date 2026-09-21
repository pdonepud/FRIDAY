"""Tests for the async Anthropic seam (``agent.claude``).

Mocking pattern mirrors ``tests/test_tts.py``: the top-level client is a
``MagicMock``; the async surface — the context manager returned by
``client.messages.stream(...)`` — is wired per-test with ``AsyncMock``
``__aenter__`` / ``__aexit__`` and a real async generator for the inner
``text_stream``. No real network calls; no real ``AsyncAnthropic``
construction inside these tests (the ``mock_claude_client`` fixture
replaces ``_get_client`` entirely; the autouse
``_reset_claude_client_cache`` fixture belts the module cache).
"""

from __future__ import annotations

import warnings
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from anthropic import RateLimitError

import agent.claude

# ---------------------------------------------------------------------------
# Fake infrastructure
# ---------------------------------------------------------------------------


async def _aiter(items: list[str]) -> AsyncIterator[str]:
    """Async-iterate a synchronous list of strings."""
    for item in items:
        yield item


async def _empty_aiter() -> AsyncIterator[str]:
    """Async iterator that yields nothing (empty stream)."""
    if False:  # pragma: no cover
        yield ""


def _mk_stream_ctx(text_source: AsyncIterator[str]) -> MagicMock:
    """Build a MagicMock async context manager whose __aenter__ returns a
    stream-like object with ``.text_stream`` set to ``text_source``.

    Returns the mock; tests can inspect ``.__aenter__`` and ``.__aexit__``
    call state via the standard ``AsyncMock`` API.
    """
    ctx = MagicMock(name="AsyncMessageStreamManager")
    inner = MagicMock(name="AsyncMessageStream")
    inner.text_stream = text_source
    ctx.__aenter__ = AsyncMock(return_value=inner)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


# ---------------------------------------------------------------------------
# Bundle 1: _get_client (lazy + cache)
# ---------------------------------------------------------------------------


def test_get_client_is_lazy(monkeypatch):
    """The module-level client cache starts as ``None``.

    ``agent/claude.py`` builds the ``AsyncAnthropic`` client on first
    call to ``_get_client`` — not at import. This lets the smoke
    tests run without an ``ANTHROPIC_API_KEY`` set and keeps
    import-time side effects to zero (see AGENT.md's swap-friendly
    architecture).
    """
    monkeypatch.setattr(agent.claude, "_client", None)
    assert agent.claude._client is None


def test_get_client_caches_instance(monkeypatch):
    """Repeated ``_get_client()`` calls return the same client instance.

    Verifies the lazy-cache pattern actually caches — the
    ``AsyncAnthropic`` constructor is invoked once and subsequent
    calls return the same object.
    """
    monkeypatch.setattr(agent.claude, "_client", None)
    fake_client = MagicMock(name="AsyncAnthropic()")
    fake_ctor = MagicMock(return_value=fake_client)
    monkeypatch.setattr("agent.claude.AsyncAnthropic", fake_ctor)

    first = agent.claude._get_client()
    second = agent.claude._get_client()

    assert first is second is fake_client
    fake_ctor.assert_called_once()


# ---------------------------------------------------------------------------
# Bundle 2: stream_tokens
# ---------------------------------------------------------------------------


async def test_stream_tokens_yields_text_chunks(mock_claude_client):
    """With the client mocked, ``stream_tokens`` yields the chunks in order.

    Verifies the seam's contract: given a mock ``AsyncAnthropic``
    whose ``messages.stream(...)`` context yields two text chunks,
    the seam surfaces the same two chunks to the caller.
    """
    ctx = _mk_stream_ctx(_aiter(["hel", "lo"]))
    mock_claude_client.messages.stream.return_value = ctx

    chunks = [
        c async for c in agent.claude.stream_tokens([{"role": "user", "content": "hi"}], "sys")
    ]

    assert chunks == ["hel", "lo"]
    mock_claude_client.messages.stream.assert_called_once()


async def test_stream_tokens_propagates_mid_stream_error(mock_claude_client):
    """A ``RateLimitError`` raised by the SDK's stream propagates through
    ``stream_tokens`` unwrapped, preserving exception identity.
    """
    sentinel = RateLimitError(
        "simulated rate limit",
        response=MagicMock(status_code=429, headers={}),
        body=None,
    )

    async def _raising_iter() -> AsyncIterator[str]:
        yield "first "
        yield "second "
        raise sentinel

    ctx = _mk_stream_ctx(_raising_iter())
    mock_claude_client.messages.stream.return_value = ctx

    got: list[str] = []
    with pytest.raises(RateLimitError) as exc_info:
        async for c in agent.claude.stream_tokens([{"role": "user", "content": "hi"}], "sys"):
            got.append(c)

    assert got == ["first ", "second "]
    assert exc_info.value is sentinel


async def test_stream_tokens_consumer_break_calls_aexit(mock_claude_client):
    """Consumer break inside ``async for`` triggers the stream context's
    ``__aexit__`` (SDK ``AsyncMessageStreamManager.__aexit__`` calls
    ``close()`` unconditionally, per
    ``anthropic/lib/streaming/_messages.py:229-243, 329-336``).

    Explicit ``gen.aclose()`` after break forces the async-generator
    machinery to unwind synchronously — otherwise cleanup is deferred
    to the event loop's shutdown hooks and would run after the test's
    assertions. Mirrors ``tests/test_tts.py``'s consumer-early-exit
    pattern.

    Asserts (a) ``__aexit__`` mock invoked, (b) no ``ResourceWarning``
    raised during teardown.
    """
    ctx = _mk_stream_ctx(_aiter(["a ", "b ", "c."]))
    mock_claude_client.messages.stream.return_value = ctx

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        gen = agent.claude.stream_tokens([{"role": "user", "content": "hi"}], "sys")
        async for _c in gen:
            break
        await gen.aclose()

    assert ctx.__aexit__.called, "async context manager __aexit__ was not invoked"
    resource_warnings = [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert not resource_warnings, (
        f"ResourceWarnings during teardown: {[str(w.message) for w in resource_warnings]}"
    )


# ---------------------------------------------------------------------------
# Bundle 3: _buffer_sentences (pure transform, no SDK)
# ---------------------------------------------------------------------------


async def test_buffer_sentences_basic_sentence_split():
    """Two-sentence input across two deltas → two chunks split at the
    sentence boundary.

    Zero-width lookahead in ``_SENTENCE_BOUNDARY`` means the trailing
    whitespace after the ``.`` goes to the NEXT chunk (leading-space
    on chunk 2), not to the current chunk (option (a) semantics).
    """
    got = [c async for c in agent.claude._buffer_sentences(_aiter(["Hello. ", "There. "]))]
    assert got == ["Hello.", " There. "]


async def test_buffer_sentences_multi_boundary_one_delta():
    """A single delta containing multiple boundaries flushes each one.

    Proves the ``while True`` boundary-flush loop iterates until no
    more boundaries match, not just once per delta.
    """
    got = [c async for c in agent.claude._buffer_sentences(_aiter(["Hello. So then, done."]))]
    assert got == ["Hello.", " So then, ", "done."]


async def test_buffer_sentences_whitespace_only_delta_skipped():
    """A whitespace-only delta doesn't yield a whitespace-only chunk.

    The buffer accumulates whitespace but never emits a chunk that
    is only whitespace (``chunk.strip()`` filter on every yield).
    """
    got = [c async for c in agent.claude._buffer_sentences(_aiter(["Hi", " ", " there."]))]
    for chunk in got:
        assert chunk.strip(), f"whitespace-only chunk leaked: {chunk!r}"
    assert " " not in got


async def test_buffer_sentences_safety_valve_next_whitespace():
    """Safety valve flushes at next whitespace at or after
    ``_MAX_BUFFER_CHARS``.

    Input is longer than the threshold with a single whitespace shortly
    after position N; the flush should include that whitespace and
    the remainder should be buffered until close.
    """
    payload = "a" * 405 + " " + "a" * 44
    got = [c async for c in agent.claude._buffer_sentences(_aiter([payload]))]
    # First chunk: 406 chars (405 a's + one space; whitespace found at
    # index 405, chunk = buffer[:406]).
    # Second chunk: remaining 44 a's flushed at end-of-stream.
    assert len(got) == 2
    assert len(got[0]) == 406
    assert got[0] == "a" * 405 + " "
    assert got[1] == "a" * 44


async def test_buffer_sentences_clause_boundary_fires():
    """Clause boundary (``[,;:]\\s``) fires and flushes a chunk."""
    got = [c async for c in agent.claude._buffer_sentences(_aiter(["First, then second."]))]
    assert got == ["First, ", "then second."]


async def test_buffer_sentences_sentence_boundary_fires():
    """Sentence boundary fires on ``. Then``.

    Same option (a) semantics as the basic-split test.
    """
    got = [c async for c in agent.claude._buffer_sentences(_aiter(["First. Then second."]))]
    assert got == ["First.", " Then second."]


async def test_buffer_sentences_decimal_intact():
    """A decimal split across two deltas stays intact — the regex must
    not fire on ``.`` followed by a digit.

    Verifies the ``[.!?](?=\\s+[A-Z]|[A-Z])`` lookahead correctly
    rejects the digit case.
    """
    got = [
        c
        async for c in agent.claude._buffer_sentences(
            _aiter(["The value is 3.", "14 was the answer."])
        )
    ]
    assert got == ["The value is 3.14 was the answer."]
    # Extra guard: no chunk is the standalone partial "The value is 3."
    assert "The value is 3." not in got


async def test_buffer_sentences_abbrev_lowercase_stays_intact():
    """An abbreviation followed by lowercase text stays intact.

    ``"e.g."`` followed by ``" foo"`` — the ``.`` at position 3 has
    lookahead ``" foo"`` where ``\\s+[A-Z]`` fails (``f`` is
    lowercase) and ``[A-Z]`` fails (space is not upper). No flush.
    """
    got = [c async for c in agent.claude._buffer_sentences(_aiter(["e.g.", " foo bar."]))]
    assert got == ["e.g. foo bar."]
    # Extra guard: no chunk is the standalone partial "e.g." or "e.g. ".
    assert "e.g." not in got
    assert "e.g. " not in got


async def test_buffer_sentences_defensive_no_leading_space_split():
    """Defensive case: BPE drops the leading space on the second
    sentence's first delta. The ``[A-Z]`` direct-lookahead alternative
    still fires.
    """
    got = [c async for c in agent.claude._buffer_sentences(_aiter(["Hello.", "There."]))]
    assert got == ["Hello.", "There."]


async def test_buffer_sentences_normal_sentence_split_leading_space():
    """Normal case: BPE preserves the leading space on the second
    sentence's first delta. ``\\s+[A-Z]`` fires.
    """
    got = [c async for c in agent.claude._buffer_sentences(_aiter(["Hello.", " There."]))]
    assert got == ["Hello.", " There."]


async def test_buffer_sentences_empty_stream():
    """An empty token stream yields no chunks."""
    got = [c async for c in agent.claude._buffer_sentences(_empty_aiter())]
    assert got == []


async def test_buffer_sentences_no_boundary_flushed_at_close():
    """A single non-boundary delta under the safety-valve threshold is
    flushed at end-of-stream by the mandatory finalization flush.
    """
    got = [c async for c in agent.claude._buffer_sentences(_aiter(["oneverylongword"]))]
    assert got == ["oneverylongword"]


async def test_buffer_sentences_error_drops_buffer():
    """Mid-stream exception drops the buffer and re-raises.

    Chunks yielded BEFORE the raise are collected; the buffered
    remainder at raise-time is dropped, not emitted.
    """
    sentinel = RuntimeError("upstream")

    async def _bad() -> AsyncIterator[str]:
        yield "Hi. "
        yield "Hello. "
        yield "There. "
        raise sentinel

    got: list[str] = []
    with pytest.raises(RuntimeError) as exc_info:
        async for c in agent.claude._buffer_sentences(_bad()):
            got.append(c)

    assert exc_info.value is sentinel
    # Two flushes reached the consumer before the raise:
    # delta 2 flushed "Hi.", delta 3 flushed " Hello.".
    assert got == ["Hi.", " Hello."]
    # The unflushed " There. " remainder must NOT appear.
    assert not any("There" in c for c in got)


# ---------------------------------------------------------------------------
# Bundle 4: safety-valve additional coverage
# ---------------------------------------------------------------------------


async def test_buffer_sentences_trailing_whitespace_not_flushed():
    """All-whitespace token stream produces no chunks.

    The mandatory finalization flush's ``if buffer.strip():`` guard
    takes the False path when the buffer contains only whitespace at
    end-of-stream. Covers that False branch specifically.
    """
    got = [c async for c in agent.claude._buffer_sentences(_aiter([" ", "\n", " "]))]
    assert got == []


async def test_buffer_sentences_safety_valve_exact_threshold():
    """Safety valve fires when whitespace lands exactly at position
    ``_MAX_BUFFER_CHARS - 1`` (the start of the scan range).

    Verifies ``range(_MAX_BUFFER_CHARS - 1, len(buffer))`` catches
    whitespace at the very start of its range.
    """
    payload = "a" * 399 + " " + "x" * 50
    got = [c async for c in agent.claude._buffer_sentences(_aiter([payload]))]
    assert len(got) == 2
    assert got[0] == "a" * 399 + " "
    assert len(got[0]) == 400
    assert got[1] == "x" * 50


# ---------------------------------------------------------------------------
# Bundle 5: stream_sentences composition
# ---------------------------------------------------------------------------


async def test_stream_sentences_composes_over_stream_tokens(monkeypatch):
    """``stream_sentences`` composes ``_buffer_sentences`` over
    ``stream_tokens`` — verified by monkeypatching ``stream_tokens``.

    Expected output matches ``_buffer_sentences``' behavior on the
    same token sequence (option (a) semantics).
    """

    async def _fake_stream_tokens(messages, system):
        yield "Hi. "
        yield "There. "

    monkeypatch.setattr("agent.claude.stream_tokens", _fake_stream_tokens)

    got = [
        c async for c in agent.claude.stream_sentences([{"role": "user", "content": "hi"}], "sys")
    ]

    assert got == ["Hi.", " There. "]


async def test_stream_sentences_consumer_break_closes_provider(monkeypatch, mock_claude_client):
    """Consumer breaking out of stream_sentences must close the underlying provider stream promptly.

    Regression guard for CodeRabbit finding on PR #63: without the finally/aclose in
    stream_sentences, the inner stream_tokens generator is left dangling and the
    provider context stays open until GC finalization. Explicit gen.aclose() after
    break forces the async-generator machinery to unwind synchronously so the
    assertion catches the invariant.
    """
    ctx = _mk_stream_ctx(_aiter(["Hello. ", "There. "]))
    mock_claude_client.messages.stream.return_value = ctx

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        gen = agent.claude.stream_sentences([{"role": "user", "content": "hi"}], "sys")
        async for _c in gen:
            break
        await gen.aclose()

    assert ctx.__aexit__.called, (
        "provider __aexit__ was not invoked after stream_sentences.aclose()"
    )
    resource_warnings = [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert not resource_warnings, (
        f"ResourceWarnings during teardown: {[str(w.message) for w in resource_warnings]}"
    )
