"""Thin seam over the Anthropic SDK — async surface.

Sole importer of ``anthropic`` per AGENT.md's swap-friendly
architecture: this is the ONLY module in the codebase that talks to
the SDK. All other modules consume ``stream_tokens`` (token-level,
used by text mode) or ``stream_sentences`` (sentence/clause-chunked,
used by the voice pipeline in #53), plus the re-exported exception
classes.

The client is constructed lazily on first use so importing this
module has no side effects and does not require ANTHROPIC_API_KEY to
be set — the smoke tests in ``tests/`` depend on that. See
``_get_client`` for the event-loop-binding caveat.

## Sentence chunker regex

Both the sentence-boundary and clause-boundary regexes are defensive
refinements of ADR-0003 §"Streaming end-to-end"'s spec (``[.!?]\\s+``
and ``[,;:]\\s+``).

The SDK's ``text_stream`` yields ``chunk.delta.text`` verbatim from
the Anthropic SSE stream (see
``anthropic/lib/streaming/_messages.py:296-299``); no whitespace
normalization is applied. Empirically, Claude's BPE tokens preserve
leading whitespace on word-initial tokens (`" There"`, not `"There"`)
in almost all observed cases, so the raw ADR regex would fire in
practice — but the invariant is not documented by Anthropic. The
refined regex handles both the leading-space case and the defensive
no-space case, and it does not over-fire on decimals (`3.14`) or
buffer-ends without a continuation (which are covered by the
mandatory finalization flush at generator close, not by the
boundary regex).
"""

import re
from collections.abc import AsyncIterator

from anthropic import (
    APIConnectionError,
    AsyncAnthropic,
    AuthenticationError,
    RateLimitError,
)

from agent.models import MODEL

__all__ = [
    "APIConnectionError",
    "AuthenticationError",
    "RateLimitError",
    "stream_sentences",
    "stream_tokens",
]

_MAX_TOKENS: int = 1024

# Safety valve for pathological output: prose walls without punctuation
# (code blocks, ASCII art, long URLs). Inference basis — typical Claude
# prose punctuates every ~75-125 chars per clause; three consecutive
# unpunctuated clauses ≈ 225 chars, so 400 sits comfortably above
# normal-flow max as a safety valve, not a soft cap on normal output.
#
# User-visible cost when the valve fires: the buffer holds silently
# until 400 chars accumulate, then flushes at the next whitespace
# (option b). At Claude Sonnet 4.6's typical output rate (~200-400
# chars/sec, not independently verified from Anthropic docs), the
# silence before the flush is roughly 1-2 seconds. Bumping this
# constant proportionally increases the worst-case silence — if you
# raise it, measure. Never lower it into the range where normal prose
# would trip it.
_MAX_BUFFER_CHARS: int = 400

# Sentence boundary: [.!?] followed by (a) whitespace + capital or
# (b) capital directly. See module docstring for the regex refinement
# rationale.
_SENTENCE_BOUNDARY = re.compile(r"[.!?](?=\s+[A-Z]|[A-Z])")

# Clause boundary: [,;:] followed by a single whitespace character.
# ADR-0003's original `\s+` collapsed here to `\s` — Claude's SSE
# deltas never emit multi-space runs between clauses.
_CLAUSE_BOUNDARY = re.compile(r"[,;:]\s")


_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    """Return the shared AsyncAnthropic client, constructing it on first use.

    Lazy so importing this module has no side effects and does not
    require ``ANTHROPIC_API_KEY`` at import time.

    IMPORTANT: The cached instance binds to whichever asyncio event
    loop it's first used in. Reusing an ``AsyncAnthropic`` across
    ``asyncio.run(...)`` calls (as pytest-asyncio auto-mode does by
    default — each test gets a fresh loop) will fail with a runtime
    error from the underlying httpx2 client's connection pool.

    Tests must either reset ``_client`` before each test or
    monkeypatch ``_get_client`` entirely. The autouse
    ``_reset_claude_client_cache`` fixture in ``tests/conftest.py``
    handles this belt-and-suspenders.
    """
    global _client
    if _client is None:
        _client = AsyncAnthropic()
    return _client


async def stream_tokens(messages: list[dict], system: str) -> AsyncIterator[str]:
    """Async token-level stream of Claude's reply.

    Yields text deltas as they arrive on the SSE stream. Chunk sizes
    vary with the server's tokenization — typically 1-30 chars each.
    Preserves the streaming-token-output contract that ADR-0003
    §"Text mode preserved" locks in for ``--text`` mode.

    Args:
        messages: full conversation so far in Anthropic message format
            (list of {"role": "user"|"assistant", "content": str}).
        system: system prompt string.

    Yields:
        Text chunks in arrival order. Concatenating every chunk gives
        the complete reply text.

    Raises:
        anthropic.AuthenticationError: bad or missing API key.
        anthropic.RateLimitError: quota exhausted.
        anthropic.APIConnectionError: network problem reaching the API.
        anthropic.APIError: other API-side failures.
    """
    client = _get_client()
    async with client.messages.stream(
        model=MODEL,
        max_tokens=_MAX_TOKENS,
        system=system,
        messages=messages,
    ) as stream:
        async for text in stream.text_stream:
            yield text


async def _buffer_sentences(tokens: AsyncIterator[str]) -> AsyncIterator[str]:
    """Buffer text deltas; flush at sentence/clause boundaries.

    Pure transform over any ``AsyncIterator[str]`` — no SDK dependency;
    tests can drive it with a plain async generator.

    Flush conditions (in check order):
      1. Sentence boundary: ``_SENTENCE_BOUNDARY`` matches (``[.!?]``
         followed by ``\\s+[A-Z]`` or ``[A-Z]``).
      2. Clause boundary: ``_CLAUSE_BOUNDARY`` matches (``[,;:]\\s``).
      3. Safety valve: buffer length ≥ ``_MAX_BUFFER_CHARS`` AND the
         buffer contains a whitespace character — flush up to and
         including the first whitespace at or after the threshold.
      4. Mandatory finalization: the token iterator has closed; any
         non-whitespace remainder is yielded verbatim.

    Whitespace-only chunks are never yielded downstream — the flush
    slice is ``.strip()``-checked before yielding.

    On mid-stream exception from ``tokens``: buffer contents are
    dropped and the exception propagates (no user-visible impact in
    this PR since voice isn't wired; see PR description for the
    #55/Tier-4 gap).
    """
    buffer = ""

    async for delta in tokens:
        buffer += delta

        # Flush all completed boundaries within the current buffer.
        # A single delta can contain multiple boundaries (e.g.
        # ". So then, ") — hence the while-loop, not a single check.
        while True:
            m_sentence = _SENTENCE_BOUNDARY.search(buffer)
            m_clause = _CLAUSE_BOUNDARY.search(buffer)
            # Pick the earliest boundary in the buffer.
            candidates = [m for m in (m_sentence, m_clause) if m is not None]
            if not candidates:
                break
            m = min(candidates, key=lambda match: match.end())
            chunk = buffer[: m.end()]
            buffer = buffer[m.end() :]
            if chunk.strip():
                yield chunk

        # Safety valve — buffer is past the threshold and no boundary
        # regex fired. Look for the next whitespace at or after position
        # N and flush there.
        if len(buffer) >= _MAX_BUFFER_CHARS:
            for i in range(_MAX_BUFFER_CHARS - 1, len(buffer)):
                if buffer[i].isspace():
                    chunk = buffer[: i + 1]
                    buffer = buffer[i + 1 :]
                    if chunk.strip():
                        yield chunk
                    break
            # If no whitespace at or after position N-1, buffer stays and
            # waits for more.

    # Mandatory finalization flush — the token iterator has closed;
    # emit any non-whitespace remainder.
    if buffer.strip():
        yield buffer


async def stream_sentences(messages: list[dict], system: str) -> AsyncIterator[str]:
    """Async sentence/clause-chunked stream of Claude's reply.

    Composes ``_buffer_sentences`` over ``stream_tokens``. Yields
    chunks at sentence and clause boundaries suitable for feeding to
    ElevenLabs' streaming TTS input.

    ## Why sentence/clause granularity for TTS

    Chunk granularity is tuned to the ElevenLabs configuration
    recorded in ADR-0004 (``agent/tts.py``:
    ``chunk_length_schedule=[50]`` at line 87, ``auto_mode`` not sent,
    ``try_trigger_generation=True`` per frame at line 259). With
    ``auto_mode`` off and a chunk-schedule of 50 characters, feeding
    token-level input (5-10 chars per chunk) causes the model to
    stall waiting for the chunk-schedule minimum to be met, per the
    ElevenLabs latency-optimization docs
    (https://elevenlabs.io/docs/eleven-api/guides/how-to/best-practices/latency-optimization).
    Feeding sentence/clause chunks (typically 25+ chars, most
    exceeding 50) triggers generation on almost every chunk. This is
    why we flush on ``[.!?]`` and ``[,;:]`` boundaries here rather
    than passing tokens through unchanged.

    Args and Raises: identical to :func:`stream_tokens`.

    Yields:
        Sentence and clause chunks in order. Concatenating every
        chunk gives the complete reply text.
    """
    async for chunk in _buffer_sentences(stream_tokens(messages, system)):
        yield chunk
