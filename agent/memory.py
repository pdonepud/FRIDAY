"""SQLite memory store (ADR-0006 §Long-term: SQLite via aiosqlite).

Sole owner of the long-term memory DB file (``<data_dir>/memory.db``)
per ADR-0002's "one small typed surface per external system" seam
pattern. This is the next instance of that pattern after
``agent/claude.py`` (Anthropic SDK), ``agent/stt.py`` (Deepgram),
``agent/tts.py`` (ElevenLabs), and the Tier-4 persistence precedent
in ``agent/session.py`` (short-term buffer JSON snapshot).

Schema v1 (base tables):

.. code-block:: sql

    CREATE TABLE memories (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,            -- ISO 8601 UTC, tz-aware
        updated_at TEXT NOT NULL,
        kind       TEXT NOT NULL,            -- 'fact', 'preference', ...
        content    TEXT NOT NULL,
        metadata   TEXT NOT NULL DEFAULT '{}' -- JSON blob
    );

    CREATE TABLE schema_version (
        version INTEGER NOT NULL
    );

Schema v2 (FTS5 keyword retrieval; #77):

.. code-block:: sql

    CREATE VIRTUAL TABLE memories_fts USING fts5(
        content,
        content='memories',
        content_rowid='id'
    );

    -- three triggers keep memories_fts in sync with memories
    CREATE TRIGGER memories_ai AFTER INSERT ON memories ...
    CREATE TRIGGER memories_ad AFTER DELETE ON memories ...
    CREATE TRIGGER memories_au AFTER UPDATE ON memories
        WHEN NEW.content IS NOT OLD.content ...

    -- backfill existing rows (v1 → v2 upgrade; no-op on fresh install)
    INSERT INTO memories_fts(memories_fts) VALUES ('rebuild');

## FTS5 capability

FTS5 is a SQLite build-time flag — most mainstream Python distributions
(python.org, Debian/Ubuntu ``python3``, Homebrew, pyenv) ship with it
enabled, but stripped builds may omit it. :func:`ensure_fts5_available`
probes the running Python's ``sqlite3`` once at :func:`bootstrap_db`
entry, BEFORE any file-system work, and raises
:class:`FTS5UnavailableError` with a remedy message. An FTS5-less
install therefore fails loudly and leaves no partial-migration residue.

## Migration pattern

Adding a v2 migration is a three-step change:

1. Append ``(2, ["<stmt1>", "<stmt2>", ...])`` to ``_MIGRATIONS``.
   Each entry is ``(version, list_of_sql_statements)``; one
   ``aiosqlite.Connection.execute`` runs per statement, inside the
   single bootstrap transaction. No embedded ``BEGIN`` / ``COMMIT`` in
   the DDL strings — the outer transaction owns both.
2. ``SCHEMA_VERSION`` updates automatically — it is defined as
   ``_MIGRATIONS[-1][0]``, so no constant needs maintaining in lockstep.
3. :func:`bootstrap_db` applies v2 on existing DBs (sees
   ``current_version == 1``, runs only the v2 entry). Fresh DBs get
   v1 + v2 in order (sees ``current_version == 0``, runs every entry).

All pending migrations run inside one Python-held
``BEGIN IMMEDIATE`` / ``COMMIT``; failure anywhere rolls the whole set
back. The ``schema_version`` row is rewritten (``DELETE`` + ``INSERT``,
UPSERT-style) inside the same transaction so the applied-version
tracker never drifts from the committed schema. ``BEGIN IMMEDIATE``
also serializes concurrent first-time ``bootstrap_db`` callers on the
same file — the loser blocks on the SQLite write lock until the winner
commits, then reads the already-applied version and skips the loop.

Downgrade (DB at a version higher than ``SCHEMA_VERSION``) raises
:class:`MemoryDBError` with both versions named. Multiple rows in the
``schema_version`` table are also treated as corruption and raise.

## Concurrency posture

Single-process, async-concurrent-only. One Python process holds the
DB; multiple ``asyncio`` tasks within that process may interleave
writes via aiosqlite's own serialization (each ``aiosqlite.Connection``
runs DB calls on a dedicated thread).

Journal mode: default DELETE. No WAL in v1 — DELETE is sufficient for
one process and keeps the file footprint to a single ``memory.db``
with no ``-wal`` / ``-shm`` siblings. If future workloads surface
``SQLITE_BUSY`` under concurrent task fan-out, a WAL migration lands
at that point.

## Connection management

Per-call ``async with aiosqlite.connect(db_path) as db`` — stateless.
No module-level singleton; loops aren't wired yet (deferred to #79 or
a follow-up, mirroring #75's Option-1 wiring posture). Promotion to a
singleton is a mechanical no-API-break change later if measurable
throughput demands it.

Public surface (ADR-0006 §public-surface, subset — ``list_memories``
is #80):

* :class:`Memory` — frozen dataclass.
* :class:`MemoryDBError` — raised for schema/migration failures.
* :class:`FTS5UnavailableError` — ``MemoryDBError`` subclass raised at
  :func:`bootstrap_db` entry when the probe fails.
* :func:`default_memory_db_path` — ``<data_dir>/memory.db``.
* :func:`ensure_fts5_available` — standalone capability probe.
* :func:`bootstrap_db` — idempotent migration runner.
* :func:`add_memory`, :func:`get_memory`, :func:`delete_memory` —
  CRUD on individual memories.
* :func:`search_memories` — ranked FTS5 keyword retrieval.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import aiosqlite

from agent.claude import classify_turn_for_memory
from agent.paths import get_data_dir

#: Filename under ``<data_dir>`` where the long-term store lives.
MEMORY_DB_FILENAME: Final[str] = "memory.db"

#: Ordered list of ``(version, list_of_statements)`` tuples. See
#: "## Migration pattern" in the module docstring for the add-a-
#: migration workflow. Each statement is run via
#: :meth:`aiosqlite.Connection.execute` inside the single bootstrap
#: ``BEGIN IMMEDIATE`` transaction.
_MIGRATIONS: Final[list[tuple[int, list[str]]]] = [
    (
        1,
        [
            """
            CREATE TABLE memories (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                kind       TEXT NOT NULL,
                content    TEXT NOT NULL,
                metadata   TEXT NOT NULL DEFAULT '{}'
            )
            """,
        ],
    ),
    (
        2,
        [
            # External-content FTS5 index backed by the memories table.
            # content_rowid='id' maps FTS rowid to memories.id so joins
            # and the 'delete' / 'rebuild' housekeeping commands line up
            # with the base table's primary key.
            "CREATE VIRTUAL TABLE memories_fts "
            "USING fts5(content, content='memories', content_rowid='id')",
            # Insert mirror. BEGIN...END trigger body is one statement.
            """
            CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
              INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
            END
            """,
            # Delete mirror using FTS5's 'delete' command — the correct
            # external-content pattern. A plain DELETE on memories_fts
            # would not work for external-content tables.
            """
            CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
              INSERT INTO memories_fts(memories_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
            END
            """,
            # Update mirror — 'delete' the old row from the index, then
            # insert the new. WHEN NEW.content IS NOT OLD.content skips
            # re-indexing on metadata-only / kind-only updates (``IS
            # NOT`` instead of ``!=`` because ``!=`` returns NULL when
            # either side is NULL, which SQLite treats as "not true" —
            # we want the trigger to fire on NULL<->value transitions).
            """
            CREATE TRIGGER memories_au AFTER UPDATE ON memories
            WHEN NEW.content IS NOT OLD.content BEGIN
              INSERT INTO memories_fts(memories_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
              INSERT INTO memories_fts(rowid, content)
                VALUES (new.id, new.content);
            END
            """,
            # Backfill existing rows on a v1→v2 upgrade. A fresh install
            # arrives with memories empty, so 'rebuild' is a trivial
            # no-op; on an upgrade it indexes everything already in
            # memories. Runs last, after the triggers exist, so the
            # resulting state is identical to one where every row had
            # been inserted post-trigger.
            "INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')",
        ],
    ),
]

#: Highest schema version the code knows how to produce. Derived from
#: :data:`_MIGRATIONS` so adding a migration bumps this automatically.
SCHEMA_VERSION: Final[int] = _MIGRATIONS[-1][0]

#: Truncation ceiling for raw-payload echoes in WARNING log lines so a
#: large corrupt-row's metadata blob doesn't flood logs.
_LOG_TRUNCATE: Final[int] = 200

#: Default k for :func:`search_memories`. ADR-0006 Implementation Notes
#: pins this to 5; context assembly (ADR-0007 / #79) may override.
_FTS_TOPK_DEFAULT: Final[int] = 5

#: Marker substring in the ``sqlite3.OperationalError`` message raised
#: when the running SQLite build lacks FTS5. Case-folded during probe
#: match to handle future SQLite capitalization drift.
_FTS5_UNAVAILABLE_MARKER: Final[str] = "no such module: fts5"

#: Metadata source tags for #78 writer audit trail — stable string
#: identifiers so the memory CLI (#80) can distinguish explicit-cue
#: writes from classifier writes, and so future dedup (out of scope)
#: has a cheap grouping key.
_CUE_SOURCE: Final[str] = "explicit_cue"
_CLASSIFIER_SOURCE: Final[str] = "classifier"

#: Compiled cue pattern for :func:`detect_explicit_cue`. Three named
#: groups map to stable ``cue_type`` identifiers that get stored in
#: each explicit-cue write's metadata.
#:
#: The ``(?:^|[\s.!?])`` prefix is a known-imperfect boundary: it
#: prevents mid-word matches but accepts whitespace-prefixed
#: subordinate-clause contexts like "I had to remember that time". We
#: ship that behavior for V1 because the natural-feeling cues ("please
#: remember that …", "ok, remember that …") depend on whitespace-
#: prefix matching. A test locks the current behavior in; tune via a
#: discourse-marker allowlist in a follow-up if false positives bite.
_CUE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"""
    (?:^|[\s.!?])
    (?:
        (?P<remember_that>remember\s+that)
        |
        (?P<take_a_note>take\s+a\s+note\s+that)
        |
        (?P<dont_forget>(?:do\s+not|don't)\s+forget(?:\s+that)?)
    )
    \s+
    (?P<content>.+?)
    \s*[.!?]*\s*$
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)

#: Strong references to in-flight classifier tasks. asyncio only
#: holds weak refs to tasks internally — without this set a task may
#: be garbage-collected mid-run. The done-callback discards entries
#: on completion so the set doesn't grow unbounded. Not thread-safe;
#: single-event-loop assumption holds throughout FRIDAY. Shutdown
#: cancellation wiring lands with #101.
_pending_tasks: set[asyncio.Task[None]] = set()

_log = logging.getLogger("agent.memory")


class MemoryDBError(Exception):
    """Raised for schema/migration failures.

    Covers downgrade (DB at version higher than :data:`SCHEMA_VERSION`)
    and corruption of the ``schema_version`` table (multiple rows).
    NOT raised for ordinary DB errors — those propagate as
    :class:`aiosqlite.Error` / :class:`sqlite3.DatabaseError`, which
    callers can catch directly.
    """


class FTS5UnavailableError(MemoryDBError):
    """FTS5 module missing from the running Python's SQLite build.

    Subclass of :class:`MemoryDBError` so a caller catching the base
    class still handles it, while a more-specific caller (e.g. a
    future install-wizard surface) can discriminate. Raised from
    :func:`ensure_fts5_available` and, transitively, from
    :func:`bootstrap_db` before any filesystem work.
    """


@dataclass(frozen=True)
class Memory:
    """One long-term memory row.

    Frozen because a Memory, once constructed, represents a specific
    state of a specific row at read time — mutating in place would
    conflate client-side state with the authoritative DB.

    Validation (``__post_init__``) enforces:

    * ``id`` is a positive int (SQLite autoincrement starts at 1).
    * ``created_at`` and ``updated_at`` are tz-aware (mirrors the
      :class:`agent.buffer.Turn` precedent).
    * ``kind`` is a non-empty string (open-set per ADR-0006 — "'fact',
      'preference', 'person', 'project', ..." are examples, not a
      closed enum).
    * ``content`` is a non-empty string.
    * ``metadata`` is a dict (serialized as JSON at the SQL boundary).
    """

    id: int
    created_at: datetime
    updated_at: datetime
    kind: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, int) or self.id <= 0:
            raise ValueError(f"Memory.id must be a positive int; got {self.id!r}")
        if self.created_at.tzinfo is None:
            raise ValueError(f"Memory.created_at must be timezone-aware; got {self.created_at!r}")
        if self.updated_at.tzinfo is None:
            raise ValueError(f"Memory.updated_at must be timezone-aware; got {self.updated_at!r}")
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError(f"Memory.kind must be a non-empty string; got {self.kind!r}")
        if not isinstance(self.content, str) or not self.content:
            raise ValueError(f"Memory.content must be a non-empty string; got {self.content!r}")
        if not isinstance(self.metadata, dict):
            raise TypeError(f"Memory.metadata must be a dict; got {type(self.metadata).__name__}")


async def ensure_fts5_available() -> None:
    """Probe the running Python's SQLite for FTS5; raise if missing.

    Opens an in-memory connection and attempts
    ``CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)``.
    A SQLite build without FTS5 raises
    :class:`sqlite3.OperationalError` with message
    ``"no such module: fts5"``; this function re-raises it as
    :class:`FTS5UnavailableError` with a remedy message. Any other
    DB error propagates unchanged.

    Idempotent and side-effect-free — safe to call repeatedly. No
    filesystem writes.
    """
    async with aiosqlite.connect(":memory:", isolation_level=None) as db:
        try:
            await db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)")
        except (sqlite3.OperationalError, aiosqlite.Error) as exc:
            if _FTS5_UNAVAILABLE_MARKER in str(exc).lower():
                raise FTS5UnavailableError(
                    "FTS5 not available in this SQLite build. Install a Python "
                    "distribution whose bundled SQLite has FTS5 enabled "
                    "(python.org installer, pyenv, Debian/Ubuntu python3, Homebrew). "
                    "See ADR-0006 §Consequences for background."
                ) from exc
            raise


def default_memory_db_path() -> Path:
    """Return ``<data_dir>/memory.db`` (platform-aware).

    Resolves via :func:`agent.paths.get_data_dir`, which bootstraps
    the data directory with the restrictive 0o700 mode on POSIX on
    first call. No other side effects.
    """
    return get_data_dir() / MEMORY_DB_FILENAME


async def bootstrap_db(db_path: Path) -> None:
    """Apply any pending migrations to the DB at ``db_path``.

    Creates the file on first call with mode 0o600 on POSIX (mirrors
    :mod:`agent.session`'s convention; the 0o700 parent dir from
    :func:`agent.paths.get_data_dir` already blocks sibling-user access,
    so this is defense-in-depth against umask-masked 0o644 inheritance).
    Idempotent — calling twice on an up-to-date DB is a no-op (the
    second call sees ``current_version == SCHEMA_VERSION`` and runs no
    migrations).

    Concurrent first-time callers on the same file are serialized via
    ``BEGIN IMMEDIATE`` — the loser waits on the SQLite write lock
    until the winner commits, then reads the already-applied version
    and skips the migration loop.

    Fails fast if FTS5 is missing (via :func:`ensure_fts5_available`)
    BEFORE any filesystem work, so an FTS5-less install leaves no
    partially-migrated ``memory.db`` behind.

    Raises:
        FTS5UnavailableError: the running Python's SQLite lacks FTS5.
        MemoryDBError: the DB is at a version higher than
            :data:`SCHEMA_VERSION` (downgrade not supported), or the
            ``schema_version`` table is corrupt (more than one row).
        aiosqlite.Error / sqlite3.DatabaseError: unrecoverable DB
            errors from a migration propagate after rollback.
    """
    # Probe FIRST — before file creation, before opening the real DB.
    # An FTS5-less install must fail with a clear remedy message
    # instead of mid-migration on the v2 CREATE VIRTUAL TABLE.
    await ensure_fts5_available()

    # Create the file with restrictive mode BEFORE aiosqlite opens it,
    # so the file's inode is born with 0o600 rather than the umask-
    # masked default (typically 0o644). Windows NTFS uses ACLs and mode
    # bits are a no-op there; the 0o700 parent dir carries the
    # guarantee on Windows.
    if not db_path.exists():
        if sys.platform == "win32":
            db_path.touch()
        else:
            fd = os.open(db_path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)

    # ``isolation_level=None`` turns off Python sqlite3's implicit
    # transaction management. On 3.11 the default behavior implicitly
    # commits any open transaction when DDL is executed, which would
    # silently break the ``BEGIN IMMEDIATE`` wrapping below (CREATE
    # TABLE memories would commit the BEGIN before the schema_version
    # update ran, breaking atomicity between the two). Autocommit mode
    # hands full control to our explicit ``BEGIN IMMEDIATE`` / COMMIT.
    async with aiosqlite.connect(db_path, isolation_level=None) as db:
        # Bootstrap the tracking table itself (idempotent — CREATE
        # TABLE IF NOT EXISTS). Committed outside the migration
        # transaction so the SELECT below has a table to query even on
        # a brand-new file.
        await db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")

        try:
            # BEGIN IMMEDIATE acquires the SQLite write lock up front
            # so concurrent bootstrap_db callers serialize here; the
            # loser then reads the already-applied version and skips
            # the migration loop. The whole transaction wraps the
            # version check AND the migrations so an exception at any
            # point rolls everything back as one unit.
            await db.execute("BEGIN IMMEDIATE")

            async with db.execute("SELECT version FROM schema_version") as cur:
                rows = await cur.fetchall()

            if len(rows) > 1:
                raise MemoryDBError(
                    f"schema_version table at {db_path} has {len(rows)} rows; refusing to proceed"
                )
            current_version = int(rows[0][0]) if rows else 0

            if current_version > SCHEMA_VERSION:
                raise MemoryDBError(
                    f"DB at {db_path} is at schema version {current_version} but code "
                    f"expects {SCHEMA_VERSION}; downgrade not supported"
                )

            applied_any = False
            for v, statements in _MIGRATIONS:
                if v <= current_version:
                    continue
                for stmt in statements:
                    await db.execute(stmt)
                current_version = v
                applied_any = True

            if applied_any:
                # DELETE + INSERT acts as UPSERT on the single-row
                # tracking table — identical shape for fresh-DB and
                # upgrade paths, and the whole pair is inside the
                # outer BEGIN IMMEDIATE so no other connection ever
                # sees an intermediate zero-row state.
                await db.execute("DELETE FROM schema_version")
                await db.execute(
                    "INSERT INTO schema_version (version) VALUES (?)",
                    (current_version,),
                )

            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def add_memory(
    db_path: Path,
    kind: str,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Insert a new memory row and return its autoincrement id.

    ``created_at`` and ``updated_at`` are assigned server-side to the
    current UTC time (equal on insert). ``metadata`` defaults to an
    empty dict and is serialized as JSON.

    Raises:
        ValueError: ``kind`` or ``content`` is empty or not a str.
            Mirrors :meth:`Memory.__post_init__`'s read-path checks so
            a bad value fails at the write site, not when a later read
            tries to construct the dataclass.
        TypeError: ``metadata`` is not a dict or None.
        UnicodeError: ``metadata`` contains a non-UTF-8-encodable
            value (e.g. a lone surrogate). Propagates from
            ``json.dumps``; no row is written.
        aiosqlite.Error / sqlite3.DatabaseError: DB errors propagate.
    """
    # Write-path validation mirroring Memory.__post_init__ — same
    # invariants, same error types, caught at the point the caller
    # provided the bad value instead of surfacing later at read time.
    if not isinstance(kind, str) or not kind:
        raise ValueError(f"kind must be a non-empty str; got {kind!r}")
    if not isinstance(content, str) or not content:
        raise ValueError(f"content must be a non-empty str; got {content!r}")
    if metadata is not None and not isinstance(metadata, dict):
        # Head off a type-smell at the seam — the DB would accept a
        # ``json.dumps([...])`` on a list, but the read path (Memory
        # construction) requires a dict and would surface this far
        # from the originating call. Fail at the write instead.
        raise TypeError(f"metadata must be a dict or None; got {type(metadata).__name__}")

    now = datetime.now(UTC)
    now_iso = now.isoformat()
    # json.dumps raises UnicodeEncodeError on lone surrogates — let it
    # propagate. Unlike session.save_session (which runs in atexit and
    # must swallow), add_memory is called from user-controllable code
    # that can handle the exception at its actual source.
    meta_json = json.dumps(metadata if metadata is not None else {}, ensure_ascii=False)

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "INSERT INTO memories (created_at, updated_at, kind, content, metadata) "
            "VALUES (?, ?, ?, ?, ?)",
            (now_iso, now_iso, kind, content, meta_json),
        )
        await db.commit()
        return int(cursor.lastrowid)


def _row_to_memory(row: aiosqlite.Row) -> Memory:
    """Parse a ``SELECT * FROM memories`` row into a :class:`Memory`.

    Pure function — no DB access, no I/O. Shared between
    :func:`get_memory` (single-row fetch) and :func:`search_memories`
    (FTS5 join) so the metadata-JSON warn-and-fall-back behavior is
    identical across both read paths.

    Corrupt ``metadata`` JSON on the row (invalid JSON, or valid JSON
    that doesn't decode to a dict) logs a WARNING naming the row id
    and returns a :class:`Memory` with ``metadata={}`` — same posture
    as :func:`agent.session.load_session`'s "warn and fall back".

    Returns a validated :class:`Memory` (``__post_init__`` runs).
    """
    raw_meta = row["metadata"]
    try:
        metadata = json.loads(raw_meta)
        if not isinstance(metadata, dict):
            raise ValueError(f"metadata is {type(metadata).__name__}, expected dict")
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        truncated = str(raw_meta)
        if len(truncated) > _LOG_TRUNCATE:
            truncated = truncated[:_LOG_TRUNCATE] + "…"
        _log.warning(
            "memory row %d has invalid metadata JSON (%s); returning empty dict. raw=%r",
            row["id"],
            exc,
            truncated,
        )
        metadata = {}

    return Memory(
        id=row["id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        kind=row["kind"],
        content=row["content"],
        metadata=metadata,
    )


async def get_memory(db_path: Path, memory_id: int) -> Memory | None:
    """Fetch a single memory by id. Returns None if no match.

    Delegates row parsing to :func:`_row_to_memory`, which handles
    corrupt-metadata warn-and-fall-back. See that function's docstring
    for details.

    Raises:
        aiosqlite.Error / sqlite3.DatabaseError: DB errors propagate.
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)) as cur:
            row = await cur.fetchone()

    if row is None:
        return None
    return _row_to_memory(row)


async def delete_memory(db_path: Path, memory_id: int) -> bool:
    """Delete the memory with the given id. Returns True if a row was
    removed, False if no row matched.

    Raises:
        aiosqlite.Error / sqlite3.DatabaseError: DB errors propagate.
    """
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        await db.commit()
        # INTEGER PRIMARY KEY makes id unique, so rowcount is 0 or 1.
        # The ``== 1`` check makes that intent explicit and treats any
        # surprising rowcount as a miss rather than a vacuous True.
        return cursor.rowcount == 1


async def search_memories(
    db_path: Path,
    query: str,
    k: int = _FTS_TOPK_DEFAULT,
) -> list[Memory]:
    """Rank the top-``k`` memories matching ``query`` via FTS5.

    Governing spec: **ADR-0006 §Retriever**. Normalizes raw
    conversational text into FTS5-safe search terms:

    1. Replaces every non-word character (operator punctuation,
       conversational punctuation ``, . ? ' ! ;``, and underscores)
       with a single space, collapsing runs. This is Unicode-aware,
       so non-ASCII letters (``café``) survive. Running this step
       FIRST exposes operator keywords that were glued by non-word
       characters (e.g. ``coffee_OR_tea``, ``coffee(AND)tea``) so
       step 2 catches them — otherwise ``\b`` word boundaries would
       miss ``_OR_`` and the keyword would leak into ``MATCH`` as an
       FTS5 operator, violating the implicit-AND guarantee.
    2. Strips the FTS5 operator keywords ``AND`` / ``OR`` / ``NOT`` /
       ``NEAR`` (whole-word, case-insensitive). ``\b`` boundaries are
       now correct because step 1 already replaced non-word glue with
       spaces. ``"ANDROID sales"`` still keeps ``ANDROID`` intact —
       the inner letters are word characters so ``\b`` doesn't match
       mid-identifier.
    3. Collapses any residual double spaces introduced by step 2's
       substitutions and strips ends.
    4. If the result is empty (e.g. a pure-noise query), returns ``[]``
       WITHOUT opening the DB.
    5. Binds the normalized terms as a BARE ``MATCH`` string (not a
       quoted phrase) — FTS5's implicit-AND over the bag of terms.

    Semantics note: ADR-0006:78 contains contradictory wording
    ("bag-of-terms keyword query" vs "quoted phrase match"). Shipped
    behavior matches the bag-of-terms intent — all terms must appear
    in the matched row, adjacency not required. A separate ADR
    correction tracks the wording fix.

    Results are ordered by FTS5's ``rank`` column (bm25 by default
    since SQLite 3.20); smaller is more relevant, so natural
    ascending order yields best-first.

    ``k`` must be a positive int. ``True`` is rejected despite
    ``isinstance(True, int)`` — this mirrors :mod:`agent.session`'s
    strict-int version check.

    Raises:
        ValueError: ``k`` is not a positive int (``None``, ``bool``,
            ``float``, string, or ``<= 0``).
        aiosqlite.Error / sqlite3.DatabaseError: DB errors propagate.
    """
    if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
        raise ValueError(f"k must be a positive int; got {k!r}")

    # Step 1: strip ALL non-word characters AND underscores, collapsing
    # runs into a single space. Running this FIRST exposes operator
    # keywords that were glued by underscores or other non-word
    # characters (e.g. ``coffee_OR_tea``, ``coffee(AND)tea``) so Step 2
    # can strip them as whole words. Previously, Step 2 would miss
    # these because ``_`` and ``(`` are not word boundaries for ``\b``,
    # and the subsequent strip would then expose the operator to FTS5
    # as an OR/AND token — leaking those semantics into what the ADR
    # promises as implicit-AND.
    # This regex also handles:
    #   (a) FTS5 operator punctuation (``* " ( ) : - ^``).
    #   (b) conversational punctuation (``, . ? ' ! ;`` and friends),
    #       which FTS5 either rejects as a parse error outside a
    #       quoted phrase or the unicode61 tokenizer treats as token
    #       glue (``coffee, tea`` → phantom adjacency).
    #   (c) underscores, which unicode61 treats as a token separator
    #       (``machine_learning`` → ``machine`` + ``learning``).
    # ``\W`` is Unicode-aware in Python 3, so letters in non-ASCII
    # scripts (e.g. ``café``) survive.
    normalized = re.sub(r"[\W_]+", " ", query).strip()
    # Step 2: whole-word operator keywords. Now runs AFTER the non-word
    # strip so underscore-exposed operators are caught. Case-insensitive
    # via re.I; ``\b`` boundaries protect mid-identifier letters
    # (``ANDROID`` still survives).
    normalized = re.sub(
        r"\b(?:AND|OR|NOT|NEAR)\b",
        " ",
        normalized,
        flags=re.IGNORECASE,
    )
    # Collapse the whitespace Step 2 may have introduced when an
    # operator keyword sat between two bare terms (``coffee OR tea`` →
    # ``coffee  tea`` after Step 2 → ``coffee tea`` here).
    normalized = re.sub(r"\s+", " ", normalized).strip()

    # Step 3: short-circuit on noise — no DB call, no log.
    if not normalized:
        return []

    async with aiosqlite.connect(db_path, isolation_level=None) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT m.id, m.created_at, m.updated_at, m.kind, m.content, m.metadata
            FROM memories m
            JOIN memories_fts f ON f.rowid = m.id
            WHERE memories_fts MATCH ?
            ORDER BY f.rank
            LIMIT ?
            """,
            (normalized, k),
        ) as cur:
            rows = await cur.fetchall()

    return [_row_to_memory(r) for r in rows]


def detect_explicit_cue(text: str) -> tuple[str, str, str] | None:
    """Detect an explicit "remember X" cue in a user turn.

    Returns ``(kind, content, cue_type)`` on a match:

    * ``kind`` — always ``"fact"`` for explicit-cue writes. The
      classifier path (if it also fires on the same turn) does any
      finer typing.
    * ``content`` — the extracted text after the cue, with trailing
      sentence-terminating punctuation and surrounding whitespace
      stripped.
    * ``cue_type`` — a stable identifier (``"remember_that"``,
      ``"take_a_note"``, or ``"dont_forget"``) for the metadata audit
      trail and future dedup grouping.

    Returns ``None`` if no cue matches or the extracted content is
    empty after stripping.

    Known imperfection: the boundary rule is whitespace-permissive,
    so subordinate-clause contexts like "I had to remember that time"
    WILL match. We ship that for V1 because tightening it would
    reject natural cues like "please remember that X" and "ok,
    remember that X". A follow-up may introduce a discourse-marker
    allowlist if the false positives bite in practice.

    Pure function. No I/O, no DB access. Modality-agnostic per
    ADR-0006 §Writer — identical behavior on voice-STT transcripts
    and typed text.
    """
    m = _CUE_PATTERN.search(text)
    if m is None:
        return None
    content = m.group("content").strip()
    content = content.rstrip(".!?").strip()
    if not content:
        return None
    if m.group("remember_that"):
        cue_type = "remember_that"
    elif m.group("take_a_note"):
        cue_type = "take_a_note"
    else:
        cue_type = "dont_forget"
    return ("fact", content, cue_type)


async def _classify_and_write(
    db_path: Path,
    user_turn: str,
    prior_assistant: str | None,
) -> None:
    """Background task body — classify, then write if durable.

    Swallows every non-cancel exception with a WARNING log; never
    re-raises into the event loop (an unhandled task exception
    surfaces as a confusing "Task exception was never retrieved"
    warning). :class:`asyncio.CancelledError` propagates so #101's
    future shutdown-cancel flow can tear down pending tasks cleanly.
    """
    try:
        result = await classify_turn_for_memory(user_turn, prior_assistant)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — writer never surfaces to user
        _log.warning("classifier task: classify call failed: %s", exc)
        return
    if result is None:
        return
    try:
        # Our audit tags clobber any model-returned metadata so the
        # provenance record is tamper-resistant from the model's
        # perspective.
        metadata = {
            **result.metadata,
            "source": _CLASSIFIER_SOURCE,
            "model": "claude-haiku-5-5",
        }
        await add_memory(
            db_path,
            kind=result.kind,
            content=result.content,
            metadata=metadata,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — writer never surfaces to user
        _log.warning("classifier task: add_memory failed: %s", exc)


def schedule_memory_classification(
    db_path: Path,
    user_turn: str,
    prior_assistant: str | None = None,
    *,
    task_registry: set[asyncio.Task[None]] | None = None,
) -> asyncio.Task[None]:
    """Spawn a fire-and-forget classifier task.

    Returns the spawned :class:`asyncio.Task`. Callers are NOT
    required to await it — voice/text loops never will. The task is
    added to a strong-reference registry (``task_registry`` if given,
    else the module-level :data:`_pending_tasks`) with a done-callback
    that discards on completion. Without this strong reference,
    asyncio can garbage-collect the task mid-run because its internal
    task set holds weak references only.

    ``task_registry`` injection point exists so tests can assert on
    registry membership without touching the module global.

    The task body in :func:`_classify_and_write` swallows all
    non-cancel exceptions per ADR-0006 §Writer.
    """
    registry = task_registry if task_registry is not None else _pending_tasks
    task = asyncio.create_task(_classify_and_write(db_path, user_turn, prior_assistant))
    registry.add(task)
    task.add_done_callback(registry.discard)
    return task


async def process_turn_for_memory(
    db_path: Path,
    user_turn: str,
    prior_assistant: str | None = None,
    *,
    task_registry: set[asyncio.Task[None]] | None = None,
) -> tuple[int | None, asyncio.Task[None]]:
    """Run both writer paths for a single user turn.

    Synchronous path: :func:`detect_explicit_cue` on ``user_turn``.
    On a match, awaits :func:`add_memory` immediately so the row
    lands within the turn (AC 1 of #78). A DB failure logs a WARNING
    and returns ``None`` for the explicit id so the caller can decide
    whether to retry or move on.

    Background path: always spawns a classifier task via
    :func:`schedule_memory_classification`. Returns the task so tests
    can await it; the loops never will.

    Returns ``(explicit_memory_id_or_None, classifier_task)``.

    Modality-agnostic — identical behavior whether called from
    ``voice_loop`` or ``text_loop`` (loop wiring lands in #101).
    """
    explicit_id: int | None = None
    cue = detect_explicit_cue(user_turn)
    if cue is not None:
        kind, content, cue_type = cue
        try:
            explicit_id = await add_memory(
                db_path,
                kind=kind,
                content=content,
                metadata={
                    "source": _CUE_SOURCE,
                    "cue_type": cue_type,
                    "cue_phrase": user_turn,
                },
            )
        except Exception as exc:  # noqa: BLE001 — writer never surfaces to user
            _log.warning("explicit-cue write failed: %s", exc)
            explicit_id = None
    classifier_task = schedule_memory_classification(
        db_path,
        user_turn,
        prior_assistant,
        task_registry=task_registry,
    )
    return (explicit_id, classifier_task)
