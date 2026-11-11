"""Context assembly for Anthropic requests (ADR-0007).

Takes the current user turn, the short-term :class:`agent.buffer.ConversationBuffer`,
and the base system prompt, and returns an
``(assembled_system, messages)`` tuple ready to splat into
:meth:`anthropic.AsyncAnthropic.messages.create`
``(system=..., messages=...)``.

Four-tier priority order from ADR-0007 §Assembly order (highest first):

1. **System prompt** — base ``base_system`` text. Always full.
2. **Current user turn** — never pruned. If it alone exceeds the overall
   budget a WARNING logs and assembly sends anyway (ADR-0007 Decision 2).
3. **Top-K retrieved long-term memories** — via
   :func:`agent.memory.search_memories`. Lower-ranked memories drop first
   if they don't fit. Injected into the system prompt as a bulleted
   "Things I remember about Preetam:" section.
4. **Recent short-term buffer turns** — chronological order preserved.
   Oldest Turns drop first; enforces BOTH the overall 8000-token soft
   budget (via a 20% safety margin → 6400-token prune target) AND the
   independent 4000-token buffer soft cap from ADR-0006 §Short-term
   (also under the 20% margin → 3200-token buffer prune target).

## Scope

* Assembler is pure after retrieval — the only I/O is the single
  ``search_memories`` call at the top. The budget loop is deterministic,
  synchronous, and testable with fixtures.
* Does NOT call the model.
* Does NOT decide what to remember (that's the writer, ADR-0006 §Writer).
* Does NOT rank memories (that's FTS5's job per ADR-0006 §Retriever).
* Does NOT maintain the buffer (that's :mod:`agent.buffer`'s job per
  ADR-0006 §Short-term).

## Telemetry

Emitted on :class:`logging.Logger` ``"agent.context"``.

* ``event=context_pruned`` at INFO when anything is cut. ``cause=<name>``
  carries the constraint(s) that drove pruning. Values are
  ``buffer_cap``, ``overall``, ``user_turn_overflow``. When more than one
  constraint drives a single assembly, the values are joined
  **alphabetically with ``+``** — e.g. ``cause=buffer_cap+overall``.
  Single-cause cases are just ``cause=<name>`` (no ``+``).
* ``event=budget_estimate_drift`` at WARNING on the rare case where
  final tiktoken verification of the fully assembled payload exceeds
  :data:`_CONTEXT_BUDGET_TOKENS`. Diagnostic only; the assembly is sent
  unchanged (ADR-0007 §Per-candidate token accounting).
* ``tiktoken unavailable (...); falling back to char/4 token estimates``
  at WARNING, once per process, if :func:`tiktoken.get_encoding` raises
  on cold start.
* ``memory retrieval failed during context assembly; proceeding with
  zero memories: ...`` at WARNING if ``search_memories`` raises.
* ``current turn exceeds budget (... tokens > ...); sending anyway per
  ADR-0007 Decision 2`` at WARNING if the user turn alone is over
  budget.

## Dependencies

* :mod:`tiktoken` (``cl100k_base`` encoding) for all token counting.
  ADR-0007 specifies Anthropic's ``client.messages.count_tokens`` as
  primary with tiktoken as fallback; #79 ships tiktoken-only for the
  voice-first latency UX (follow-up tracked for the Anthropic endpoint
  integration).
* First call to :func:`tiktoken.get_encoding` fetches the BPE vocab from
  Azure blob storage (~4MB, cached at tiktoken's default cache dir —
  ``<tempdir>/data-gym-cache`` unless overridden via
  ``TIKTOKEN_CACHE_DIR`` — thereafter). A network failure at first use
  degrades to ``max(1, len(text) // 4)`` estimation via the
  ``_encoder_available`` flag, not a crash.
* :func:`warm_encoder` runs the fetch in a worker thread under
  :func:`asyncio.wait_for` so startup doesn't stall the event loop on
  a cold cache (relevant for ``voice_loop`` where a stalled main loop
  would freeze the PTT pump, first-chunk waiter, playback task, and
  barge-in detector).

## Signature deviation from ADR-0007

ADR-0007 §Module location gives the signature as
``async def build_messages(current_turn: str) -> tuple[str, list[dict]]``
— no explicit ``buffer`` or ``base_system`` parameters. Shipped here as
the three-param form so the module stays testable without module-level
patching of :mod:`agent.system_prompt` or a loop's buffer global. A
follow-up tracks the ADR wording update.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Final

import tiktoken

from agent.buffer import ConversationBuffer, Turn
from agent.memory import default_memory_db_path, search_memories

#: Overall per-turn token budget. See ADR-0007 §Target budget.
_CONTEXT_BUDGET_TOKENS: Final[int] = 8000  # TODO(#79-tuning)

#: Independent soft cap on the short-term buffer portion. ADR-0006
#: §Short-term + ADR-0007 §Assembly item 4 — both limits apply; whichever
#: binds first drives pruning.
_BUFFER_TOKEN_CAP: Final[int] = 4000  # TODO(#79-tuning)

#: Top-K retrieved long-term memories. Matches
#: :data:`agent.memory._FTS_TOPK_DEFAULT`; bumping either is a tuning
#: knob.
_MEMORY_TOPK: Final[int] = 5  # TODO(#79-tuning)

#: Safety margin applied to both the overall budget and the buffer cap
#: during per-candidate pruning. Compensates for tiktoken's documented
#: ~15-20% undercount on typical English text (ADR-0007 §Token counting).
_SAFETY_MARGIN_PCT: Final[float] = 0.20  # TODO(#79-tuning)

#: Pre-computed pruning targets (hot path avoids repeated float math).
_PRUNE_TARGET: Final[int] = int(_CONTEXT_BUDGET_TOKENS * (1 - _SAFETY_MARGIN_PCT))
_BUFFER_PRUNE_TARGET: Final[int] = int(_BUFFER_TOKEN_CAP * (1 - _SAFETY_MARGIN_PCT))

#: Per-message overhead for the Anthropic Messages API — role tokens,
#: delimiter structure, etc. Anthropic doesn't publish an exact number;
#: 4 is the widely-used estimate for OpenAI-shaped chat formats and is
#: close enough under the 20% safety margin. If
#: ``budget_estimate_drift`` telemetry starts firing frequently, widen
#: the safety margin or integrate the Anthropic ``count_tokens``
#: endpoint via the follow-up issue. TODO(#79-tuning).
_ROLE_OVERHEAD_TOKENS: Final[int] = 4

#: Memory-injection section header. Exact wording from ADR-0007
#: §Assembly item 3.
_MEMORY_SECTION_HEADER: Final[str] = "Things I remember about Preetam:"

#: tiktoken encoding name (ADR-0007 §Token counting).
_TIKTOKEN_ENCODING: Final[str] = "cl100k_base"

#: Upper bound for :func:`warm_encoder`'s async wait on the first
#: tiktoken fetch. 15 s is a reasonable one-time startup hang bound;
#: connections slower than that are probably better off in the char/4
#: fallback anyway. TODO(#79-tuning).
_ENCODER_WARM_TIMEOUT_S: Final[float] = 15.0

_log = logging.getLogger("agent.context")

# Module-level lazy encoder cache. ``_encoder_available`` is False once
# we've logged the fallback WARNING, so the warn fires exactly once per
# process (idempotent for repeated ``_count_tokens`` calls).
_encoder: tiktoken.Encoding | None = None
_encoder_available: bool = True


def _get_encoder() -> tiktoken.Encoding | None:
    """Lazy-load the ``cl100k_base`` encoding, caching the instance.

    On first failure (network error / packaging issue / anything else),
    logs a WARNING once and flips ``_encoder_available`` so subsequent
    :func:`_count_tokens` calls short-circuit to char/4 without
    re-raising.
    """
    global _encoder, _encoder_available
    if _encoder is not None:
        return _encoder
    if not _encoder_available:
        return None
    try:
        _encoder = tiktoken.get_encoding(_TIKTOKEN_ENCODING)
    except Exception as exc:  # noqa: BLE001 — tiktoken init failure must not crash context assembly  # noqa: E501
        _encoder_available = False
        _log.warning(
            "tiktoken unavailable (%s); falling back to char/4 token estimates",
            exc,
        )
        return None
    return _encoder


async def warm_encoder(timeout: float = _ENCODER_WARM_TIMEOUT_S) -> None:
    """Prefetch the tiktoken encoding off the event loop.

    Runs :func:`tiktoken.get_encoding` in a worker thread via
    :func:`asyncio.to_thread`, bounded by :func:`asyncio.wait_for` so
    a cold cache's ~4 MB vocab download doesn't block the event loop.
    Relevant for :func:`agent.loop.voice_loop` where a stalled main
    loop would freeze the PTT pump, the first-chunk waiter, the
    playback task, and the barge-in detector all at once.

    Idempotent in both directions:

    * If :data:`_encoder` is already populated, returns immediately.
    * If :data:`_encoder_available` is already ``False`` (a prior
      attempt failed), returns immediately — no retry.

    On ``TimeoutError`` from :func:`asyncio.wait_for` OR any exception
    from the underlying :func:`tiktoken.get_encoding` call, flips
    :data:`_encoder_available` to ``False`` and logs the one-time
    WARNING via :func:`_get_encoder`'s existing guard (identical
    message shape, matched by every log-asserting test) so subsequent
    :func:`_count_tokens` calls degrade to char/4 estimation.

    Known interaction: :func:`asyncio.wait_for` cannot cancel a sync
    function running in :func:`asyncio.to_thread` — the background
    thread keeps running after the timeout fires and may eventually
    populate ``_encoder`` later. That's accepted because
    :func:`_get_encoder`'s ``if _encoder is not None`` check will
    transparently promote the late result on the next
    :func:`_count_tokens` call. The warmup timeout only decides
    whether STARTUP blocks, not whether tiktoken is ever used.
    """
    global _encoder, _encoder_available
    if _encoder is not None:
        return
    if not _encoder_available:
        return
    try:
        _encoder = await asyncio.wait_for(
            asyncio.to_thread(tiktoken.get_encoding, _TIKTOKEN_ENCODING),
            timeout=timeout,
        )
    except TimeoutError:
        _encoder_available = False
        _log.warning(
            "tiktoken unavailable (warmup timed out after %.1fs); "
            "falling back to char/4 token estimates",
            timeout,
        )
    except Exception as exc:  # noqa: BLE001 — warmup failure must not crash startup
        _encoder_available = False
        _log.warning(
            "tiktoken unavailable (%s); falling back to char/4 token estimates",
            exc,
        )


def _count_tokens(text: str) -> int:
    """Count tokens in ``text`` using tiktoken, with char/4 fallback.

    Returns 0 for empty text. For non-empty text under the fallback,
    returns ``max(1, len(text) // 4)`` so the pruning algorithm never
    sees a zero cost on a non-empty candidate.
    """
    if not text:
        return 0
    enc = _get_encoder()
    if enc is None:
        return max(1, len(text) // 4)
    # #79 CR F1: ``enc.encode(text)`` defaults to
    # ``disallowed_special="all"`` and raises ValueError on any literal
    # tiktoken special-token string in the input (``<|endoftext|>``,
    # ``<|fim_prefix|>``, ``<|endofprompt|>``, …). Every input to
    # :func:`build_messages` flows through here — a user who types any
    # of those strings would (a) brick ``text_loop`` because the
    # ValueError escapes outside Phase 5's try block AFTER the user
    # Turn was appended and the writer ran, poisoning session.json and
    # re-crashing on every subsequent load until FIFO eviction drops
    # it, and (b) lose the turn in ``voice_loop`` via its generic
    # safety-net ``except``. ``encode_ordinary`` treats the input as
    # plain bytes end-to-end, which is exactly what a length-only
    # counter wants — special-token recognition is dead weight here.
    return len(enc.encode_ordinary(text))


def _format_memory_section(memories: list) -> str:
    """Return the memory-injection block, or empty string on no memories.

    Empty-memory case omits the header entirely to avoid a dangling
    "Things I remember about Preetam:" with nothing under it.
    """
    if not memories:
        return ""
    lines = [_MEMORY_SECTION_HEADER]
    lines.extend(f"- {m.content}" for m in memories)
    return "\n".join(lines)


def _prune_memories(memories: list, budget_left: int) -> tuple[list, int]:
    """Drop lowest-ranked memories until the section fits ``budget_left``.

    Memories arrive bm25-ranked (highest first) from
    :func:`search_memories`, so iterate front-to-back and keep while
    the section's estimated cost stays within the budget. Returns
    ``(kept, dropped_count)``.
    """
    if not memories:
        return ([], 0)
    kept: list = []
    header_cost = _count_tokens(_MEMORY_SECTION_HEADER)
    section_cost = 0
    for m in memories:
        bullet_cost = _count_tokens(f"- {m.content}")
        # The header's cost only applies once we keep at least one
        # memory; a newline separator between header and first bullet
        # adds a trivial 1-2 tokens handled under the safety margin.
        next_cost = (header_cost if not kept else 0) + bullet_cost
        if section_cost + next_cost > budget_left:
            break
        kept.append(m)
        section_cost += next_cost
    return (kept, len(memories) - len(kept))


def _prune_buffer_turns(
    prior_turns: list[Turn],
    overall_left: int,
    buffer_cap: int,
) -> tuple[list[Turn], int]:
    """Drop OLDEST Turns until BOTH caps fit; strip leading non-user.

    ``prior_turns`` is in oldest-to-newest order (iteration order of
    :class:`agent.buffer.ConversationBuffer`). Each Turn's cost is
    estimated as ``_count_tokens(Turn.content) + _ROLE_OVERHEAD_TOKENS``.

    Drops oldest first until:

    * ``sum(kept_costs) <= buffer_cap`` (the per-buffer soft cap), AND
    * ``sum(kept_costs) <= overall_left`` (what remains of the overall
      budget after fixed-cost + memory-section).

    Then strips any leading non-user Turn from the kept slice — this is
    the role-alternation defense that used to live in
    ``agent/loop.py::_as_user_first_messages`` for the #101-CR-R2 F2
    FIFO-overflow case.

    Returns ``(kept, dropped_count)``. ``dropped_count`` includes both
    the budget-pruned tail-of-the-front AND any leading-assistant
    prefix stripped afterwards.
    """
    costs = [_count_tokens(t.content) + _ROLE_OVERHEAD_TOKENS for t in prior_turns]
    kept = list(prior_turns)
    kept_costs = list(costs)
    # Drop oldest (index 0) until both caps are met.
    while kept and (sum(kept_costs) > buffer_cap or sum(kept_costs) > overall_left):
        kept.pop(0)
        kept_costs.pop(0)
    # Strip any leading non-user Turn (role-alternation defense).
    while kept and kept[0].role != "user":
        kept.pop(0)
    return (kept, len(prior_turns) - len(kept))


async def build_messages(
    current_turn: str,
    buffer: ConversationBuffer,
    base_system: str,
) -> tuple[str, list[dict]]:
    """Assemble the ``(system, messages)`` pair for an Anthropic request.

    ADR-0007-governed four-tier priority: system > current turn > top-K
    memories > recent buffer. All pruning uses tiktoken estimates with
    a 20% safety margin; final verification re-counts the assembled
    payload and emits :ref:`budget_estimate_drift` on exceedance (no
    re-pruning per ADR-0007).

    ``buffer`` is expected to carry the just-appended user Turn at
    ``buffer[-1]`` per #101's loop wiring; the assembler reads
    ``list(buffer)[:-1]`` as the prior history and reconstructs the
    final user message from ``current_turn``.

    Memory-retrieval failures (anything ``search_memories`` raises) log
    a WARNING and the assembly proceeds with zero memories — memory
    failures never break the conversation (ADR-0006 §Writer directive
    extended to the read path).

    Raises:
        asyncio.CancelledError: propagates for shutdown teardown.
    """
    # --- 1. Retrieval (the one I/O) ---------------------------------
    try:
        memories = await search_memories(default_memory_db_path(), current_turn, k=_MEMORY_TOPK)
    except Exception as exc:  # noqa: BLE001 — retrieval failure must not break conversation  # noqa: E501
        _log.warning(
            "memory retrieval failed during context assembly; proceeding with zero memories: %s",
            exc,
        )
        memories = []

    memories_in = len(memories)
    prior_turns = list(buffer)[:-1]
    buffer_turns_in = len(prior_turns)

    # --- 2. Fixed cost (C4: include user-turn role overhead) --------
    # Final verification counts _ROLE_OVERHEAD_TOKENS for the current
    # user turn too (it's a message in the list). Including it here
    # keeps the per-candidate budget symmetric with final verification
    # per ADR-0007 §Per-candidate token accounting — asymmetry would
    # silently eat the 20% safety margin.
    base_system_tokens = _count_tokens(base_system)
    current_turn_tokens = _count_tokens(current_turn)
    fixed = base_system_tokens + current_turn_tokens + _ROLE_OVERHEAD_TOKENS

    # --- 3. User-turn-exceeds-budget guard --------------------------
    user_turn_overflow = current_turn_tokens > _CONTEXT_BUDGET_TOKENS
    if user_turn_overflow:
        _log.warning(
            "current turn exceeds budget (%d tokens > %d); sending anyway per ADR-0007 Decision 2",
            current_turn_tokens,
            _CONTEXT_BUDGET_TOKENS,
        )

    # --- 4. Memory selection ----------------------------------------
    # Memory gets whatever's left after the fixed cost. When the user
    # turn overflows, skip memories entirely — the overflow warning
    # already committed us to sending-anyway.
    if user_turn_overflow:
        kept_memories: list = []
        memories_dropped = memories_in
    else:
        memory_budget = max(0, _PRUNE_TARGET - fixed)
        kept_memories, memories_dropped = _prune_memories(memories, memory_budget)

    memory_section = _format_memory_section(kept_memories)
    memory_section_tokens = _count_tokens(memory_section) if memory_section else 0

    # --- 5. Buffer selection ----------------------------------------
    if user_turn_overflow:
        kept_buffer: list[Turn] = []
        buffer_dropped = buffer_turns_in
    else:
        overall_left = max(0, _PRUNE_TARGET - fixed - memory_section_tokens)
        kept_buffer, buffer_dropped = _prune_buffer_turns(
            prior_turns,
            overall_left=overall_left,
            buffer_cap=_BUFFER_PRUNE_TARGET,
        )

    # --- 6. Assemble ------------------------------------------------
    assembled_system = f"{base_system}\n\n{memory_section}" if memory_section else base_system
    messages: list[dict] = [t.to_message() for t in kept_buffer]
    messages.append({"role": "user", "content": current_turn})

    # --- 7. Final verification (tiktoken-only per D4) ---------------
    verified_tokens = _count_tokens(assembled_system) + sum(
        _count_tokens(m["content"]) + _ROLE_OVERHEAD_TOKENS for m in messages
    )
    if verified_tokens > _CONTEXT_BUDGET_TOKENS:
        _log.warning(
            "event=budget_estimate_drift estimated=%d verified=%d budget=%d margin_pct=%s",
            fixed
            + memory_section_tokens
            + sum(_count_tokens(t.content) + _ROLE_OVERHEAD_TOKENS for t in kept_buffer),
            verified_tokens,
            _CONTEXT_BUDGET_TOKENS,
            _SAFETY_MARGIN_PCT,
        )

    # --- 8. Pruning telemetry ---------------------------------------
    anything_cut = memories_dropped > 0 or buffer_dropped > 0 or user_turn_overflow
    if anything_cut:
        causes: list[str] = []
        if user_turn_overflow:
            causes.append("user_turn_overflow")
        else:
            # Which cap bound first? Compute both predicates against
            # the pre-prune state so the telemetry is self-explanatory
            # even when both bound. C1: alphabetical join.
            if buffer_dropped > 0:
                pre_buffer_cost = sum(
                    _count_tokens(t.content) + _ROLE_OVERHEAD_TOKENS for t in prior_turns
                )
                if pre_buffer_cost > _BUFFER_PRUNE_TARGET:
                    causes.append("buffer_cap")
                if memories_dropped > 0 or (
                    fixed + memory_section_tokens + pre_buffer_cost > _PRUNE_TARGET
                ):
                    causes.append("overall")
            elif memories_dropped > 0:
                causes.append("overall")
        # Deduplicate + sort alphabetically (C1).
        causes_str = "+".join(sorted(set(causes))) if causes else "overall"
        _log.info(
            "event=context_pruned memories_in=%d memories_kept=%d "
            "buffer_turns_in=%d buffer_turns_kept=%d estimated_tokens=%d "
            "budget=%d prune_target=%d cause=%s",
            memories_in,
            len(kept_memories),
            buffer_turns_in,
            len(kept_buffer),
            verified_tokens,
            _CONTEXT_BUDGET_TOKENS,
            _PRUNE_TARGET,
            causes_str,
        )

    return (assembled_system, messages)
