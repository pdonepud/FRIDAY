# ADR 0007 — Context assembly and token budget

**Status:** Proposed
**Date:** 2026-10-05

## Context

ADR-0006 introduces a short-term conversation buffer and a long-term SQLite memory store. The LLM request for every turn is now assembled from four sources instead of two:

1. System prompt (persona, instructions, static)
2. Retrieved long-term memories (variable count, variable size)
3. Short-term buffer turns (variable count, variable size)
4. The user's current turn (always present)

Without discipline, the message list grows unboundedly. Sonnet's 200k context window is generous but not free — cost scales with input tokens, and large contexts hurt latency and accuracy ("context rot" at the tail). We need a deterministic, measurable, tunable assembly rule.

## Decision

### Target budget

- **8,000 tokens of input context per turn.**
- Chosen as a conservative upper bound: well under Sonnet's limit, keeps per-turn cost predictable, keeps latency acceptable for a voice-first UX.
- The budget is a module-level constant (`_CONTEXT_BUDGET_TOKENS = 8000`) with a `TODO(#NN-tuning)` comment. Expected to move; the point is that one number controls it.

### Assembly order (highest priority first)

Each component is appended in order; whatever budget remains after a higher-priority component is what the next component has to work with.

1. **System prompt.** Fixed. Measured once at startup. Always present in full.
2. **User's current turn.** Always present in full. Never pruned. If a single user turn exceeds the remaining budget, assembly logs a warning and sends it anyway — truncating the user's words is worse than exceeding budget.
3. **Top-K retrieved long-term memories.** K defaults to 5. Memories are retrieved via `search_memories(current_turn_text, k=5)` from ADR-0006. Injected into the system prompt as a bulleted "Things I remember about Preetam:" section. Lower-ranked memories are dropped first if the group doesn't fit.
4. **Recent short-term buffer turns.** Newest first. Oldest dropped first when the remaining budget runs out.

### Token counting

- **Primary:** Anthropic's token-counting endpoint (`client.messages.count_tokens`). Accurate, matches billing.
- **Fallback:** `tiktoken` with the `cl100k_base` encoding as a rough approximation if the endpoint is unavailable. Logs a warning. Approximation is within ~10% of actual for typical text — acceptable for budgeting.
- Counting happens once per assembly, cached per-request. Not re-counted per candidate component (would be expensive).
- Pre-sized estimates (characters / 4) are not used — too lossy for a budget-critical path.

### Pruning telemetry

When anything is cut:

One line, structured enough to grep, tuned to help us notice when the budget is wrong.

### Module location and purity

- `agent/context.py` owns assembly.
- Public surface: `async def build_messages(current_turn: str) -> list[dict]`.
- Pure after retrieval — no I/O inside the budget loop. Memory retrieval runs **before** assembly, in parallel with STT finalization once the user's text is known. This hides retrieval latency behind STT latency we already pay.
- No LLM call inside assembly. Assembly is deterministic, synchronous after retrieval, and testable with fixtures.

### What assembly does NOT do

- Does not call the model.
- Does not decide what to remember (that is the writer, ADR-0006).
- Does not rank memories (that is FTS5's job, ADR-0006).
- Does not maintain the buffer (that is `agent/buffer.py`'s job, ADR-0006).

Assembly is a reducer: inputs → one `list[dict]` ready for `messages.create()`.

## Consequences

**Positive:**

- Every LLM request has a knowable upper bound on input size.
- Pruning is deterministic and auditable. When we hit "wait, why did FRIDAY forget that," the telemetry says exactly what got dropped.
- Priority order codifies our values: never truncate the user's words, prefer explicit memory to older buffer turns.
- One constant (`_CONTEXT_BUDGET_TOKENS`) controls the whole system; easy to adjust once we have real usage data.

**Negative:**

- Budget is a knob and will need tuning. Starting at 8k is a guess — may be too tight for conversations referencing many memories, too loose if cost becomes a concern.
- Retrieval runs on every turn, even on "what time is it" where retrieval is pointless. Mitigation deferred: a cheap gate (user turn length, question-word detection) could skip retrieval for trivial turns. Not worth building until retrieval cost shows up as a real problem.
- Token counting adds one Anthropic API call per turn. Mitigation: fallback to `tiktoken` if latency hurts; the fallback is already in the design.
- The classifier call from ADR-0006 and the retrieval call from this ADR together add two extra round-trips per voice turn. Both are off the critical path for the user-perceived "time to first audio," but they do add to end-to-end.

**Neutral:**

- `tiktoken` becomes a dev dependency for the fallback path. Small, well-maintained.
- The 8k budget means we are deliberately leaving ~192k tokens of model capacity unused for most turns. That is fine — more context is not always better, and cost control is a real constraint for a solo project.

## Alternatives Considered

### A. No budget, send everything

Rejected:
- Cost scales linearly with conversation length.
- Latency grows with input size.
- Context rot degrades response quality on long contexts.

### B. Fixed slot sizes (e.g., 2k system, 2k memories, 3k buffer, 1k current turn)

Rejected:
- A long user turn would waste memory and buffer slots.
- A terse user turn would waste nothing but also under-use the budget.
- Priority-based allocation gets the behavior we actually want.

### C. LLM-driven summarization each turn

Idea: when the buffer gets too long, summarize older turns into a single "summary" turn. Rejected for v1:
- Adds a round-trip per turn (or every N turns), with latency and cost.
- Summarization loses fidelity exactly where we might want it (specific phrasing).
- The buffer is bounded by turn count already — if 20 turns don't fit in 8k, that's a signal to tune, not to summarize.
- Revisit in a follow-up ADR if buffer eviction visibly hurts conversation quality.

### D. Dynamic re-ranking per turn

Idea: after FTS5 returns top-k, re-rank with an LLM call to pick the best fit. Rejected:
- Another round-trip per turn.
- Keyword retrieval is already noisy; a small ranking boost does not justify the latency cost at this scale.
- Revisit if FTS5 retrieval quality is visibly poor.

### E. Separate system prompts for voice vs. text loops

Rejected — the voice loop and text loop share enough that two system prompts would drift. One prompt, maybe one or two conditional lines.

## Implementation Notes

- Issue #79 tracks implementation.
- Depends on #74 (buffer) and #77 (FTS5 retrieval).
- Pruning telemetry log line uses the project's standard logger — no new logging infrastructure.
- Token-counting function is factored so the Anthropic endpoint and the `tiktoken` fallback share a signature; swap is one line.
- Unit tests exercise:
  - Under-budget: nothing pruned, all components present.
  - Over-budget on memories: lowest-ranked memories dropped first.
  - Over-budget on buffer: oldest turns dropped first.
  - Catastrophic over-budget on user turn: warning logged, request sent anyway.
  - Empty memory results: assembly works with zero memories.
  - Empty buffer: assembly works with just system + current turn.

## Open Questions (Not Blocking)

- Is 8k the right starting number? Will know after a week of real use. Fine to tune.
- Should K (top memories retrieved) scale with available budget, or stay fixed at 5? Current decision: fixed. Revisit after real use.
- Should assembly tag which memories were included, so the memory CLI can show "last used" statistics? Nice-to-have, not required for v1.

## References

- Anthropic token-counting endpoint (`client.messages.count_tokens`): <https://docs.anthropic.com/en/api/messages-count-tokens>
- `tiktoken` repository: <https://github.com/openai/tiktoken>
- ADR-0002 — Model routing strategy (seam-pattern precedent).
- ADR-0006 — Memory architecture (buffer and FTS5 retrieval this ADR consumes).
