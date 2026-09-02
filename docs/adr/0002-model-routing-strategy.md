# ADR 0002 — Model routing strategy

**Status:** Accepted (defer LiteLLM adoption; use thin-seam abstraction until trigger conditions materialize)
**Date:** 2026-09

## Context

FRIDAY's Tier 1 baseline (PR #35) sits behind a single-function "thin seam" over the Anthropic SDK (`agent/claude.py::stream_reply`). This satisfies AGENT.md's swap-friendly architecture principle: every external provider is accessed through one small function that is the only place in the codebase that knows about that specific SDK.

A near-term architectural question surfaced during Tier 1: should FRIDAY route between multiple LLM providers, and if so, when and how? Two adjacent motivations were named:

1. **Cost optimization.** Cheap open-source models (DeepSeek, Kimi, Llama via hosts like Together, Groq, Fireworks) for classification, summarization, or routing tasks; frontier hosted models (Claude, GPT) for the main conversational path and anything touching sensitive data or irreversible actions.

2. **Capability-and-trust tiering.** A safety principle: high-trust surfaces (confirmation-gate decisions, tool selection, message drafting) always run on frontier models with strong safety training; low-trust surfaces (bulk classification, cheap summarization) can run on cheaper open models where throughput matters more than judgment.

Two implementation options were considered:

**Option A — Adopt LiteLLM now.** A focused dependency providing a unified `completion()` interface across 100+ models and providers. Own routing logic sits on top; provider abstraction comes for free.

**Option B — Extend the existing thin seam when needed.** Keep `agent/claude.py` as the sole Anthropic client. When a second provider becomes necessary, either turn `stream_reply` into a dispatcher or add a sibling function per provider and route via a small `agent/router.py`. No new dependency until concretely required.

## Decision

Adopt **Option B**. Defer LiteLLM adoption until specific trigger conditions materialize.

The thin seam already provides the abstraction LiteLLM would provide, so adopting LiteLLM now is buying an abstraction FRIDAY already has. The specific reasoning:

1. **The abstraction is redundant against the existing seam.** `agent/claude.py` is one function, one file. Provider swaps or additions happen there. LiteLLM would move that same responsibility into a different function, in a different file, with a dependency between them.

2. **LiteLLM's abstraction is leaky for the features FRIDAY will actually want.** Prompt caching, tool use, extended thinking, streaming semantics, and error taxonomy differ non-trivially across providers. LiteLLM's unified interface tends to expose the lowest common denominator; Anthropic-specific features either work partially or force dropping out of the abstraction to use the native SDK anyway.

3. **The trigger hasn't fired.** FRIDAY is a personal assistant with a single user. Cost is measured in cents per day at Sonnet rates. The capability-and-trust tiering argument is coherent but requires background classifiers, summarizers, or bulk drafters — none of which exist through Tier 3. The trigger for actually routing between providers doesn't materialize until Tier 4 (memory summarization) or Tier 5 (proactive-behavior background scoring) at the earliest.

4. **The thin seam defers this decision without penalty.** When the trigger fires, the swap or extension is contained to a small area of code — the call path stays behind the seam; the error boundary is handled per the "Error boundary" section below (either normalized in the seam, or by adjusting `agent/loop.py`).

## Trigger conditions

Revisit this ADR when *any* of the following becomes true:

- A concrete FRIDAY task requires a model that Anthropic doesn't offer (specific capability, specific cost point) — not a hypothetical "we might want" but an actual planned feature
- Background or bulk workloads emerge (memory summarization in Tier 4, proactive-behavior scoring in Tier 5) where routing that workload to a cheap model would save at least an order of magnitude of cost compared to running it on the frontier model
- Multi-provider becomes a hard requirement (a specific tool integration only works with a specific model)
- The thin seam becomes noticeably painful to maintain because it has accumulated more than 3 provider-specific branches

At the trigger, re-evaluate LiteLLM against extending the seam, with the concrete requirements in hand. This ADR may be superseded by a follow-up ADR documenting the actual adoption.

## Error boundary

The current thin seam re-exports Anthropic's exception classes (`AuthenticationError`, `RateLimitError`, `APIConnectionError`) through `agent/claude.py`, and `agent/loop.py` catches those specific classes directly. This means the seam abstracts the *call* but not the *errors* — a second provider raises different exception types, and the loop would need corresponding except clauses (or a normalization layer inside the seam) to handle them.

This is a deliberate Tier-1 tradeoff: introducing an internal exception hierarchy (e.g., `FridayModelError`, `FridayAuthError`) before there's a second provider to normalize against would be premature abstraction — designing for hypothetical shapes. The concrete cost is that adding a second provider is not purely a `claude.py` edit; it will also touch `loop.py` or introduce a normalization layer at that point. That cost is acceptable given the trigger conditions above.

When the trigger fires, one of two paths is chosen as part of the adoption ADR: (a) introduce a `FridayModelError` hierarchy inside the seam that normalizes across providers, letting `loop.py` catch stable internal types; or (b) accept that `loop.py` grows provider-aware except clauses, which is fine if the provider count stays at 3 or fewer.

## Consequences

**Positive:**
- Tier 1 ships against the smallest possible dependency set (`anthropic` + `python-dotenv` only)
- No mid-flight scope expansion; the thin seam continues to serve as the single place provider changes happen
- The decision is documented for future revisit rather than forgotten and re-litigated
- Trigger conditions are explicit, so re-evaluation is not ad hoc

**Negative:**
- If multi-provider becomes necessary sooner than expected, some routing scaffolding will need to be written by hand rather than inherited from LiteLLM
- Familiarity with LiteLLM as a tool is deferred; that experience gets built later or via a side project

**Neutral:**
- MCP (Model Context Protocol) remains the intended path for external service integrations (Gmail, Calendar, etc.) — this ADR is about LLM providers specifically, not about all external integrations

## References

- `AGENT.md` — swap-friendly architecture principle
- `agent/claude.py` — the current thin seam
- PR #35 — Tier 1 baseline that established the seam
- ADR 0001 — voice-first pivot (context for why single-user cost patterns dominate)
