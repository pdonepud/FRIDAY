"""SQLite memory store scaffolding (ADR-0006 §Long-term: SQLite via aiosqlite).

Sole owner of the long-term memory DB file (``<data_dir>/memory.db``)
per ADR-0002's "one small typed surface per external system" seam
pattern. This is the next instance of that pattern after
``agent/claude.py`` (Anthropic SDK), ``agent/stt.py`` (Deepgram),
``agent/tts.py`` (ElevenLabs), and the Tier-4 persistence precedent
in ``agent/session.py`` (short-term buffer JSON snapshot).

Schema v1:

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

FTS5 virtual table + triggers are explicitly deferred to #77 — per the
ADR §Consequences capability-probe note, #77 owns both the probe and
the FTS layer. Keeping v1 FTS5-free means the ``memory.db`` file
itself is portable to minimal / stripped Python builds without FTS5
support; v2 (#77) is where that portability becomes conditional.

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

Public surface (ADR-0006 §public-surface, subset — ``search_memories``
is #77; ``list_memories`` is #80):

* :class:`Memory` — frozen dataclass.
* :class:`MemoryDBError` — raised for schema/migration failures only.
* :func:`default_memory_db_path` — ``<data_dir>/memory.db``.
* :func:`bootstrap_db` — idempotent migration runner.
* :func:`add_memory`, :func:`get_memory`, :func:`delete_memory` —
  CRUD on individual memories.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import aiosqlite

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
]

#: Highest schema version the code knows how to produce. Derived from
#: :data:`_MIGRATIONS` so adding a migration bumps this automatically.
SCHEMA_VERSION: Final[int] = _MIGRATIONS[-1][0]

#: Truncation ceiling for raw-payload echoes in WARNING log lines so a
#: large corrupt-row's metadata blob doesn't flood logs.
_LOG_TRUNCATE: Final[int] = 200

_log = logging.getLogger("agent.memory")


class MemoryDBError(Exception):
    """Raised for schema/migration failures.

    Covers downgrade (DB at version higher than :data:`SCHEMA_VERSION`)
    and corruption of the ``schema_version`` table (multiple rows).
    NOT raised for ordinary DB errors — those propagate as
    :class:`aiosqlite.Error` / :class:`sqlite3.DatabaseError`, which
    callers can catch directly.
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

    Raises:
        MemoryDBError: the DB is at a version higher than
            :data:`SCHEMA_VERSION` (downgrade not supported), or the
            ``schema_version`` table is corrupt (more than one row).
        aiosqlite.Error / sqlite3.DatabaseError: unrecoverable DB
            errors from a migration propagate after rollback.
    """
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


async def get_memory(db_path: Path, memory_id: int) -> Memory | None:
    """Fetch a single memory by id. Returns None if no match.

    Corrupt ``metadata`` JSON on the row (invalid JSON, or valid JSON
    that doesn't decode to a dict) logs a WARNING and the returned
    :class:`Memory` has ``metadata={}`` — same "warn and fall back"
    posture as :func:`agent.session.load_session`.

    Raises:
        aiosqlite.Error / sqlite3.DatabaseError: DB errors propagate.
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)) as cur:
            row = await cur.fetchone()

    if row is None:
        return None

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
            memory_id,
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
