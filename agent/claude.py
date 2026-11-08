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

import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Final

import httpx2
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
    "MemoryClassification",
    "NetworkError",
    "RateLimitError",
    "classify_turn_for_memory",
    "stream_sentences",
    "stream_tokens",
]

_log = logging.getLogger("agent.claude")


class NetworkError(Exception):
    """Transport-level failure during LLM streaming (#55).

    Wraps ``httpx2.TransportError`` so ``agent.loop`` can catch an
    ``agent.claude``-owned type without importing ``httpx2`` directly.
    Keeps ADR-0003's LLM seam intact: only ``agent.claude`` touches
    ``anthropic``/``httpx2``.

    Covers mid-stream SSE drops (``RemoteProtocolError``, ``ReadError``,
    ``ReadTimeout``, ``ConnectError``, and other ``TransportError``
    subclasses) that the SDK does NOT translate into
    ``anthropic.APIConnectionError`` — the SDK's wrapping lives at
    ``_base_client._request`` and runs at connect-time only, not mid-SSE.
    See #55 investigation §5 for the empirical trace.
    """


_MAX_TOKENS: int = 1024

# #78 memory writer — classifier knobs. Smallest capable Claude per
# ADR-0006 Implementation Notes (classifier model is deliberately
# not routed through agent/models.py; that constant stays the single
# source of truth for the main-conversation model per Tier 1 §15).
# TODO(#78-tuning): revisit if extraction quality is poor in practice.
_CLASSIFIER_MODEL: Final[str] = "claude-haiku-5-5"
_CLASSIFIER_MAX_TOKENS: Final[int] = 512
_CLASSIFIER_TIMEOUT_SECONDS: Final[float] = 10.0

# Frozen prompt. A snapshot test asserts it does not drift silently —
# the classifier's output distribution is prompt-dependent, so any
# rewrite should be a visible test change.
_CLASSIFIER_SYSTEM_PROMPT: Final[str] = (
    "You decide whether a user's turn contains a durable fact worth "
    "remembering long-term for a personal assistant named FRIDAY. "
    "Durable means: preferences, relationships, projects, facts the "
    "user has stated about themselves or their world — the kind of "
    "thing a close colleague would remember. Not durable: small talk, "
    "momentary feelings, questions, acknowledgements, requests for "
    "action.\n\n"
    "Respond with ONE JSON object and nothing else. On a durable fact:\n"
    '  {"remember": true, "kind": "<fact|preference|person|project>", '
    '"content": "<concise third-person statement>", "metadata": {}}\n'
    "On no durable fact:\n"
    '  {"remember": false}\n\n'
    'Keep "content" short (under 160 chars), third-person, declarative. '
    "Never include the user's name — the storage layer handles ownership."
)

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
    try:
        async with client.messages.stream(
            model=MODEL,
            max_tokens=_MAX_TOKENS,
            system=system,
            messages=messages,
        ) as stream:
            async for text in stream.text_stream:
                yield text
    except httpx2.TransportError as e:
        # #55 D4: mid-stream SSE drops (TCP reset, DNS flap, read
        # timeout, …) bubble out of the SDK as raw httpx2.* types —
        # the SDK's APIConnectionError wrap only runs at connect-time.
        # Translate at the seam so agent.loop can catch
        # agent.claude.NetworkError without importing httpx2.
        raise NetworkError(str(e)) from e


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
        # N and flush there. Drain repeatedly to handle single deltas
        # that contain multiple threshold-crossing sections; without the
        # outer while, one flush per delta leaves oversized content for
        # the finalization flush to emit verbatim.
        while len(buffer) >= _MAX_BUFFER_CHARS:
            flushed = False
            for i in range(_MAX_BUFFER_CHARS - 1, len(buffer)):
                if buffer[i].isspace():
                    chunk = buffer[: i + 1]
                    buffer = buffer[i + 1 :]
                    if chunk.strip():
                        yield chunk
                    flushed = True
                    break
            if not flushed:
                # No whitespace beyond N-1; buffer stays and waits for
                # more (mandatory flush at end-of-stream handles the
                # pathological no-whitespace case).
                break

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
    tokens = stream_tokens(messages, system)
    try:
        try:
            async for chunk in _buffer_sentences(tokens):
                yield chunk
        except httpx2.TransportError as e:
            # #55 D4: mirror stream_tokens' translation here so a
            # drop that surfaces inside _buffer_sentences' iteration
            # (which drives tokens.__anext__) is caught at the seam.
            # stream_tokens already wraps its own direct SSE
            # iteration; this covers the case where the drop is
            # observed by the sentence buffer's consumer side.
            raise NetworkError(str(e)) from e
    finally:
        await tokens.aclose()


@dataclass(frozen=True)
class MemoryClassification:
    """Structured result from :func:`classify_turn_for_memory`.

    Returned when the classifier decides a turn contains a durable
    fact. Fields mirror :class:`agent.memory.Memory` so the writer
    scheduler can feed directly into :func:`agent.memory.add_memory`.
    """

    kind: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


async def classify_turn_for_memory(
    user_turn: str,
    prior_assistant: str | None = None,
) -> MemoryClassification | None:
    """Ask Claude whether ``user_turn`` contains a durable fact.

    Returns a :class:`MemoryClassification` on
    ``{"remember": true, ...}`` with a valid ``kind`` + ``content``,
    or ``None`` on ``{"remember": false}``, malformed JSON, invalid
    structure, empty response, or any API error. All failure modes
    log a WARNING under ``agent.claude`` and return ``None`` — callers
    do NOT need to catch exceptions. This matches ADR-0006 §Writer's
    "Failures log and do not surface to the user" directive at the
    actual failure site.

    ``prior_assistant``, when provided, is included in the user
    message as additional context (ADR-0006 open question: starting
    assumption is latest user turn + prior assistant turn).

    One-shot non-streaming call with a 10s request timeout and a 512
    max-tokens cap. Model: :data:`_CLASSIFIER_MODEL`.
    """
    if prior_assistant is not None:
        user_msg = f"Prior assistant: {prior_assistant}\nUser turn: {user_turn}"
    else:
        user_msg = f"User turn: {user_turn}"

    try:
        client = _get_client()
        response = await client.messages.create(
            model=_CLASSIFIER_MODEL,
            max_tokens=_CLASSIFIER_MAX_TOKENS,
            system=_CLASSIFIER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            timeout=_CLASSIFIER_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 — writer never surfaces to user
        # Broad Exception rather than anthropic.APIError specifically:
        # the SDK can also surface httpx errors on connection drops
        # (same shape as #55 D4 for mid-stream). Log + return None.
        _log.warning("classifier call failed: %s", exc)
        return None

    if not response.content:
        _log.warning("classifier returned empty content")
        return None

    raw = response.content[0].text.strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        truncated = raw if len(raw) <= 200 else raw[:200] + "…"
        _log.warning(
            "classifier returned malformed JSON (%s); raw=%r",
            exc,
            truncated,
        )
        return None

    if not isinstance(payload, dict):
        _log.warning(
            "classifier payload is not an object; got %s",
            type(payload).__name__,
        )
        return None

    if payload.get("remember") is not True:
        # Explicit false or missing — "nothing to save" is not a failure.
        return None

    kind = payload.get("kind")
    content = payload.get("content")
    if not isinstance(kind, str) or not kind:
        _log.warning(
            "classifier remember=true but kind is missing/empty; payload=%r",
            payload,
        )
        return None
    if not isinstance(content, str) or not content:
        _log.warning(
            "classifier remember=true but content is missing/empty; payload=%r",
            payload,
        )
        return None

    metadata = payload.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}

    return MemoryClassification(kind=kind, content=content, metadata=metadata)
