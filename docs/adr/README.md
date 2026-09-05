# Architecture Decision Records

This directory holds Architecture Decision Records (ADRs) for FRIDAY. An ADR captures a significant architectural or design decision, the context that produced it, and the consequences that follow — so future contributors (including future-me) can understand *why* the code looks the way it does, not just what it does.

## Format

Each ADR follows a lightly-adapted Nygard structure:

- **Status** — Proposed / Accepted / Superseded (by which ADR) / Deprecated
- **Context** — The situation and forces at play when the decision was made
- **Decision** — What was decided
- **Consequences** — What follows (positive, negative, neutral); what becomes easier and what becomes harder

ADRs are numbered sequentially starting at 0001. Filenames follow `NNNN-kebab-case-title.md`. Once accepted, ADRs are immutable — a superseding decision goes in a new ADR that references the old one.

## Index

- [0001 — Voice-first pivot](./0001-voice-first-pivot.md) — Accepted
- [0002 — Model routing strategy](./0002-model-routing-strategy.md) — Accepted (deferred adoption)
- [0003 — Voice architecture](./0003-voice-architecture.md) — Accepted

## When to write an ADR

Any decision that:
- Affects the shape of the system in ways that would be hard to reverse later
- A future reader would ask "why did they do it this way?" about
- Involves choosing between multiple credible options with tradeoffs
- Deliberately *defers* another decision (documenting *why* deferring is the right call)

Small implementation choices don't need ADRs. Architectural direction changes always do.
