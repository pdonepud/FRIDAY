# ADR 0006 — Memory architecture

**Status:** Accepted
**Date:** 2026-10-05

## Context

Through Tier 3, FRIDAY has no state across turns beyond what the Anthropic API carries in the message list for a single process lifetime. Nothing survives process restart. For FRIDAY to behave as a companion rather than a session-scoped assistant, she needs to remember two distinct shapes of information:

1. **Short-term conversation context** — recent turns, verbatim, so that references like "what we just talked about" or "that thing from this morning" resolve correctly within and across sessions.
2. **Long-term memory** — discrete facts, preferences, relationships, project context — things explicitly worth keeping for weeks or months, retrievable by topic.

These needs are different enough in access pattern, retention, and shape that a single store serves them badly. Short-term is sequential and small; long-term is keyed, searchable, and grows unboundedly.

The project is single-user, local-first, no cloud sync, no server. Dependencies should be minimal and well-maintained.

## Decision

### Short-term: bounded in-memory buffer, persisted to JSON

- **Shape:** `list[Turn]` where `Turn` is a dataclass of `(role, content, timestamp)`.
- **Bounds:** configurable turn-count ceiling (default 20) and a token soft cap (default 4,000 tokens, enforced by context assembly per ADR-0007). Eviction is FIFO on turn count; token-aware pruning is ADR-0007's job.
- **Persistence:** serialized to `<data_dir>/session.json` on clean shutdown and `atexit`. Loaded on startup. Atomic write via temp file + `os.replace`. On corrupt, unreadable (non-ENOENT), or bad-schema file, log a WARNING and start empty. A missing file (first run) is treated as a clean-install signal and silently starts empty with no log. Pathological parse failures from adversarial input — a deeply-nested JSON array raising `RecursionError`, for example — are NOT caught by the loader and propagate unchanged. `session.json` is written by FRIDAY itself, so this trade-off is acceptable under the current single-user threat model; file a hardening issue if that assumption changes.
- **Location:** `agent/buffer.py` (or `agent/memory.py` if we fold both there — implementation decides).
- **Ownership:** both `voice_loop` and `text_loop` append to the buffer; context assembly reads. The buffer is loop-agnostic conversational context — a user who tests in text mode and switches to voice (or vice versa) should see FRIDAY remember what they just said. No module other than the two loops writes.

### Long-term: SQLite via aiosqlite

- **Backend:** SQLite file at `<data_dir>/memory.db`, accessed via `aiosqlite`. aiosqlite is a thin async wrapper around stdlib `sqlite3`, actively maintained, no transitive runtime deps.
- **Schema v1:**
```sql
  CREATE TABLE memories (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      created_at TEXT NOT NULL,            -- ISO 8601 UTC
      updated_at TEXT NOT NULL,
      kind       TEXT NOT NULL,            -- 'fact', 'preference', 'person', 'project', ...
      content    TEXT NOT NULL,
      metadata   TEXT NOT NULL DEFAULT '{}' -- JSON blob
  );

  CREATE VIRTUAL TABLE memories_fts USING fts5(
      content,
      content='memories',
      content_rowid='id'
  );

  CREATE TABLE schema_version (
      version INTEGER NOT NULL
  );
```
  FTS5 is kept in sync with insert/update/delete triggers (standard FTS5 pattern).

- **Migration posture:** `schema_version` table from day one. A migration runner applies ordered migrations idempotently on startup. v1 is the initial baseline; future ADRs may add v2+.

- **Module ownership:** `agent/memory.py` is the sole DB access point. Mirrors the ADR-0002 seam pattern: one small, typed surface per external system. No raw SQL outside `agent/memory.py`.

- **Public surface (v1):**
```python
  async def add_memory(kind: str, content: str, metadata: dict | None = None) -> int
  async def get_memory(id: int) -> Memory | None
  async def delete_memory(id: int) -> bool
  async def search_memories(query: str, k: int = 5) -> list[Memory]
  async def list_memories(kind: str | None = None, limit: int = 100) -> list[Memory]
```

### Writer: hybrid (explicit + classifier)

Two paths write to long-term memory:

1. **Explicit cue** — the user says "remember that X", "take a note that X", "don't forget X". A lightweight phrase detector runs on turn text from both `voice_loop` and `text_loop` and writes directly on a match. Modality-agnostic by design: the detector is a text pattern match, not a voice feature, so it works identically on turns transcribed from STT or typed into the text REPL. Deterministic, instant, user-controlled. The matching logic lives in a shared helper (e.g., `agent/memory.py::detect_explicit_cue()`) called from both loops to prevent drift between the two paths.
2. **Post-turn classifier** — after each turn resolves (in either loop), a background task hands the turn to a small Claude call via the `agent/claude.py` seam, asking "is there a durable fact worth remembering here?" and expecting a structured response. Non-blocking by design in both loops, though the constraint carries different weight: in `voice_loop` it is critical because a blocking classifier would starve TTS playback or delay the next PTT turn, producing audible gaps; in `text_loop` it is a responsiveness concern — the user shouldn't wait on classification before being able to type the next turn. Same fire-and-forget scheduling pattern in both, lives in a shared helper alongside `detect_explicit_cue()`. Failures log and do not surface to the user.

The two paths co-exist. Explicit cues always write. Classifier writes are additive and can be audited/corrected via the memory CLI (issue #80).

### Retriever: FTS5 keyword, k=5 default

- Called by context assembly (ADR-0007) with the user's current turn as the query.
- `search_memories` accepts raw conversational text — context assembly does NOT need to know FTS5 syntax. Internally, `search_memories` normalizes the input before passing to `MATCH`:
  1. Replaces every non-word character (operator punctuation, conversational punctuation, underscores) with a single space (Unicode-aware; preserves non-ASCII letters like `café`).
  2. Strips the FTS5 operator keywords `AND`/`OR`/`NOT`/`NEAR` as whole words, case-insensitive.
  3. Collapses whitespace, lowercases, splits into terms, drops a module-level English stopword list (articles, copulas, pronouns, wh-words, common prepositions, modal auxiliaries, conversational fillers like `tell`/`about`/`please`, and contraction residues left by step 1), dedupes while preserving first-seen order.
  4. If no terms remain, returns `[]` without opening the DB.
  5. Builds the `MATCH` expression as `"t1" OR "t2" OR …` with each term double-quoted (injection-safe because every term is `\w+` after step 1; quoting also neutralizes any operator keyword that slipped past step 2).
  6. `memories_fts` uses `tokenize='porter unicode61'` (schema v3); porter stemming is applied to both stored and query terms, so `name` ↔ `named`.
  7. Results are ordered by bm25 `rank` (smaller is more relevant). bm25 weighs term frequency, term rarity, and memory length, so a memory matching more query terms *tends* to rank higher — especially among short facts of similar length, which is what FRIDAY stores — but this is not guaranteed: a short single-term match can outrank a long multi-term match.
- Returns top-k ranked results. k is tunable.
- Vector embeddings are **deferred**. If keyword retrieval produces visibly poor results during real use, a follow-up ADR introduces embeddings (likely `sentence-transformers` local, or a hosted embedding model) and extends the schema.

### Data location

Platform-aware, resolved by `agent/paths.py::get_data_dir()`:

- Linux: `$XDG_DATA_HOME/friday` or `~/.local/share/friday`
- macOS: `~/Library/Application Support/friday`
- Windows: `%APPDATA%\friday`
- Fallback: `~/.friday`

Both `session.json` and `memory.db` live in this directory. First run bootstraps the directory.

## Consequences

**Positive:**

- Clear separation between conversational context (buffer) and durable knowledge (SQLite). Each is tuned to its access pattern.
- SQLite is boring, atomic, inspectable with any SQLite client, backed up by a file copy. All desirable for a solo local-first project.
- FTS5 ships with most mainstream Python distributions' stdlib `sqlite3` (python.org installers, Debian/Ubuntu packages, Homebrew Python, pyenv-built Python on macOS), with no native extensions to compile in the common case. However, FTS5 is a SQLite build-time flag — minimal or stripped Python builds may omit it. Mitigation: `agent/memory.py` runs a startup capability check (`CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)` on an in-memory connection) and fails loudly with a clear error message if FTS5 is unavailable, pointing the user at installing a Python with FTS5 support. #77 owns the probe implementation.
- `agent/memory.py` as single owner mirrors the proven ADR-0002 pattern; keeps testing tractable.
- Hybrid writer gives the user explicit control (the cue) and ambient help (the classifier) without choosing one.

**Negative:**

- First real on-disk persistent state outside of config. Introduces a backup/portability concern users will eventually hit; CLI export (issue #80) partially mitigates.
- First third-party async DB dependency (`aiosqlite`). Small surface, but a supply-chain surface nonetheless.
- Classifier writes cost LLM tokens per turn. Mitigation: use the smallest capable model, keep the prompt short, cap to one call per turn.
- Schema v1 locks assumptions. Future needs (vector embeddings, provenance tracking, user confidence scores) will require migrations.

**Neutral:**

- Memory encryption at rest is deferred. The data directory is user-scoped on all platforms; this matches current threat model.
- The buffer-vs-memory boundary may blur over time (e.g., "promote a buffer turn to a durable memory"). Addressed when it becomes real.

## Alternatives Considered

### A. Single JSONL append-only log for both

Simplest possible. Rejected:

- No indexed queries. Scanning the whole log on every turn gets slow quickly.
- No FTS. Retrieval would be substring scanning.
- Hard to update a memory in place (would require log compaction).

### B. Vector store (Chroma, LanceDB, Qdrant) from day one

Rejected:

- Additional runtime dependency, often with native code.
- Premature — we have no data to tune retrieval against yet.
- Keyword FTS5 covers the common case ("what did I say about X"). We can swap in embeddings when FTS visibly fails.

### C. Postgres or Redis

Rejected — introduces infra for a solo local-first app. Overshoots.

### D. Pickle / shelve

Rejected — pickle is fragile across Python versions and opaque to inspection.

### E. Fold short-term into SQLite as a `recent_turns` table

Rejected — the access pattern is sequential and append-heavy on a tiny dataset. In-memory list plus JSON snapshot is faster, simpler, and easier to reason about. SQLite earns its keep for the keyed, searchable long-term store.

### F. LLM-managed memory (ask Claude to decide what to remember mid-turn)

Rejected for the write path — adds latency to every turn even when nothing is worth remembering. The post-turn classifier runs out of band and gets most of the benefit without the latency cost. May revisit for the read path if FTS retrieval underperforms.

## Implementation Notes

- Tier 4 issues #73 through #80 track implementation.
- #73 (config dir) must land first.
- #74 (buffer) and #76 (SQLite scaffolding) can proceed in parallel after #73.
- #77 (FTS5) builds on #76.
- #78 (writer) depends on #76 and the `agent/claude.py` seam for the classifier call.
- #79 (context assembly, ADR-0007) consumes both #74 and #77.
- #80 (CLI) is the last piece and depends on #77.
- Default constants (`_BUFFER_TURN_LIMIT = 20`, `_FTS_TOPK_DEFAULT = 5`, `_CLASSIFIER_MODEL = ...`) belong in named module-level constants with `TODO(#NN-tuning)` comments where appropriate.

## Open Questions (Not Blocking)

- Classifier model selection: smallest Claude that reliably extracts facts. Decide at implementation time; keep it swappable.
- Should the classifier see the full recent buffer or just the latest turn? Starting assumption: latest turn + prior assistant turn for context. Revisit.
- What happens to memories when the user says "forget everything about X"? Current plan: `search_memories("X")` → confirm count with the user → bulk delete. The confirmation flow may want to wait for Tier 7's confirmation gate rather than hand-rolling it in Tier 4.

## Amendments

**2026-11-13 — Retriever semantics (via #109):** replaces implicit-AND with OR of stopword-filtered terms, ranked by bm25. Schema v3 migrates `memories_fts` to `tokenize='porter unicode61'` so stored and query terms share the same stemmer (`name` ↔ `named`). Reverses the implicit-AND decision from #77 / PR #100 / PR #103 based on the 2026-11-13 E2E evidence: implicit-AND over a natural-language turn fails on every filler word, and conversational turns include several filler words every time. See the §Retriever rewrite above for the shipped pipeline. Status moves Proposed → Accepted now that Tier 4 is complete and validated against real use.

## References

- SQLite FTS5 documentation: <https://www.sqlite.org/fts5.html>
- `aiosqlite` project page: <https://github.com/omnilib/aiosqlite>
- XDG Base Directory Specification: <https://specifications.freedesktop.org/basedir-spec/basedir-spec-latest.html>
- ADR-0002 — Model routing strategy (seam-pattern precedent this ADR follows).
