"""Tests for :mod:`agent.memory` (ADR-0006 §Long-term SQLite scaffolding)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from agent.memory import (
    SCHEMA_VERSION,
    FTS5UnavailableError,
    Memory,
    MemoryDBError,
    add_memory,
    bootstrap_db,
    delete_memory,
    ensure_fts5_available,
    get_memory,
    list_memories,
    search_memories,
)


@pytest.fixture
async def bootstrapped_db(tmp_path: Path) -> Path:
    """A ready-to-use memory.db bootstrapped to the latest schema (no rows)."""
    path = tmp_path / "memory.db"
    await bootstrap_db(path)
    return path


# -----------------------------------------------------------------
# bootstrap_db
# -----------------------------------------------------------------


async def test_bootstrap_creates_schema_on_fresh_db(tmp_path):
    """Fresh DB bootstraps to v1 schema with both tables + the
    tracking row set to SCHEMA_VERSION."""
    path = tmp_path / "memory.db"
    assert not path.exists()

    await bootstrap_db(path)

    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ) as cur:
            tables = [r["name"] for r in await cur.fetchall()]
        async with db.execute("SELECT version FROM schema_version") as cur:
            versions = [r["version"] for r in await cur.fetchall()]

    assert "memories" in tables
    assert "schema_version" in tables
    assert versions == [SCHEMA_VERSION]


async def test_bootstrap_is_idempotent_on_up_to_date_db(bootstrapped_db):
    """Second bootstrap on an already-migrated DB is a no-op — the
    schema_version table stays single-row at SCHEMA_VERSION and no
    migration runs again."""
    await bootstrap_db(bootstrapped_db)  # must not raise

    async with aiosqlite.connect(bootstrapped_db) as db:
        async with db.execute("SELECT version FROM schema_version") as cur:
            rows = await cur.fetchall()

    assert rows == [(SCHEMA_VERSION,)]


async def test_bootstrap_rejects_future_schema_version(tmp_path):
    """DB at a version higher than SCHEMA_VERSION → MemoryDBError
    naming both versions. Downgrade is not supported."""
    path = tmp_path / "memory.db"
    async with aiosqlite.connect(path) as db:
        await db.execute("CREATE TABLE schema_version (version INTEGER NOT NULL);")
        await db.execute("INSERT INTO schema_version (version) VALUES (?)", (99,))
        await db.commit()

    with pytest.raises(MemoryDBError) as exc_info:
        await bootstrap_db(path)
    msg = str(exc_info.value)
    assert "99" in msg
    assert str(SCHEMA_VERSION) in msg


async def test_bootstrap_rejects_multiple_schema_version_rows(tmp_path):
    """Multiple rows in schema_version → MemoryDBError (treated as
    corruption rather than silently picking one)."""
    path = tmp_path / "memory.db"
    async with aiosqlite.connect(path) as db:
        await db.execute("CREATE TABLE schema_version (version INTEGER NOT NULL);")
        await db.executemany("INSERT INTO schema_version (version) VALUES (?)", [(1,), (1,)])
        await db.commit()

    with pytest.raises(MemoryDBError, match="schema_version"):
        await bootstrap_db(path)


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX-only, non-root (mode bits are a no-op on Windows; root ignores them)",
)
async def test_bootstrap_creates_db_file_with_0o600_mode_on_posix(tmp_path):
    """On POSIX as non-root, bootstrap_db creates memory.db with mode
    0o600 (owner-rw only), matching session.json's convention. Defense
    in depth on top of the 0o700 parent dir from get_data_dir()."""
    path = tmp_path / "memory.db"
    assert not path.exists()

    await bootstrap_db(path)

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


async def test_concurrent_bootstrap_calls_serialize(tmp_path):
    """Two concurrent bootstrap_db calls on the same fresh file both
    succeed (no SQLITE_BUSY). BEGIN IMMEDIATE serializes the writers;
    the loser reads the already-applied version and skips the
    migration loop. Result: exactly one schema_version row at the
    current SCHEMA_VERSION, no duplicate CREATE TABLE memories."""
    path = tmp_path / "memory.db"

    await asyncio.gather(bootstrap_db(path), bootstrap_db(path))

    async with aiosqlite.connect(path) as db:
        async with db.execute("SELECT version FROM schema_version") as cur:
            rows = await cur.fetchall()
    assert rows == [(SCHEMA_VERSION,)]


async def test_bootstrap_rolls_back_on_mid_migration_failure(tmp_path, monkeypatch):
    """A migration that fails AFTER a successful CREATE TABLE must
    roll back the DDL, not leave the half-applied table behind.
    SQLite DDL is transactional, and our BEGIN IMMEDIATE wrapper plus
    isolation_level=None ensure the table creation gets undone when
    a later statement in the same migration raises."""
    path = tmp_path / "memory.db"

    # Inject a bad migration that succeeds on CREATE TABLE memories
    # then raises on a reference to a nonexistent table. The real
    # _MIGRATIONS is restored at test teardown by monkeypatch.
    broken_migrations = [
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
                "INSERT INTO __mid_migration_sentinel__ VALUES (1)",
            ],
        ),
    ]
    monkeypatch.setattr("agent.memory._MIGRATIONS", broken_migrations)

    with pytest.raises(aiosqlite.Error, match="__mid_migration_sentinel__"):
        await bootstrap_db(path)

    # Post-crash inspection: memories table must NOT exist (DDL rolled
    # back), schema_version table persists (created outside the
    # wrapper tx) but has zero rows (UPSERT never ran).
    async with aiosqlite.connect(path) as db:
        async with db.execute("SELECT name FROM sqlite_master WHERE type='table'") as cur:
            tables = {row[0] for row in await cur.fetchall()}
        async with db.execute("SELECT COUNT(*) FROM schema_version") as cur:
            (count,) = await cur.fetchone()

    assert "memories" not in tables
    assert "schema_version" in tables
    assert count == 0

    # Restore the real migrations list and retry on the same path —
    # should succeed cleanly and bring the DB to v1 with no residue.
    monkeypatch.undo()
    await bootstrap_db(path)
    async with aiosqlite.connect(path) as db:
        async with db.execute("SELECT version FROM schema_version") as cur:
            rows = await cur.fetchall()
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memories'"
        ) as cur:
            memories_row = await cur.fetchone()
    assert rows == [(SCHEMA_VERSION,)]
    assert memories_row is not None


# -----------------------------------------------------------------
# add_memory / get_memory
# -----------------------------------------------------------------


async def test_add_memory_returns_positive_int_and_sets_tz_aware_timestamps_equal(
    bootstrapped_db,
):
    """add_memory returns a positive autoincrement id; both timestamps
    are tz-aware and equal on insert."""
    before = datetime.now(UTC)
    new_id = await add_memory(bootstrapped_db, kind="fact", content="FRIDAY uses SQLite")
    after = datetime.now(UTC)

    assert new_id > 0
    mem = await get_memory(bootstrapped_db, new_id)
    assert mem is not None
    assert mem.id == new_id
    assert mem.created_at == mem.updated_at
    assert mem.created_at.tzinfo is not None
    # Clock-skew-friendly window check.
    assert before - timedelta(seconds=1) <= mem.created_at <= after + timedelta(seconds=1)


async def test_add_memory_defaults_metadata_to_empty_dict(bootstrapped_db):
    """metadata=None defaults to {} on read."""
    new_id = await add_memory(bootstrapped_db, kind="fact", content="x")
    mem = await get_memory(bootstrapped_db, new_id)
    assert mem is not None
    assert mem.metadata == {}


async def test_add_memory_round_trips_explicit_metadata(bootstrapped_db):
    """A rich metadata dict (nested, mixed types) round-trips byte-
    equal through the JSON serialization."""
    meta = {"source": "cli", "confidence": 0.9, "nested": {"k": [1, 2, 3]}}
    new_id = await add_memory(
        bootstrapped_db, kind="preference", content="prefers dark mode", metadata=meta
    )
    mem = await get_memory(bootstrapped_db, new_id)
    assert mem is not None
    assert mem.metadata == meta


async def test_add_memory_preserves_non_ascii_content(bootstrapped_db):
    """ensure_ascii=False keeps UTF-8 content intact on round-trip."""
    text = "café 🧠 日本語"
    new_id = await add_memory(bootstrapped_db, kind="fact", content=text)
    mem = await get_memory(bootstrapped_db, new_id)
    assert mem is not None
    assert mem.content == text


async def test_add_memory_rejects_metadata_with_lone_surrogate(bootstrapped_db):
    """A lone surrogate inside metadata must propagate as a
    UnicodeError from json.dumps — no row is written."""
    with pytest.raises(UnicodeError):
        await add_memory(
            bootstrapped_db,
            kind="fact",
            content="x",
            metadata={"bad": "\ud800"},
        )
    # Confirm nothing landed.
    async with aiosqlite.connect(bootstrapped_db) as db:
        async with db.execute("SELECT COUNT(*) FROM memories") as cur:
            (count,) = await cur.fetchone()
    assert count == 0


async def test_add_memory_rejects_non_dict_metadata(bootstrapped_db):
    """metadata of a wrong type (list, str, etc.) must raise TypeError
    BEFORE the DB is opened — no transaction, no partial write."""
    with pytest.raises(TypeError, match="metadata must be a dict or None"):
        await add_memory(
            bootstrapped_db,
            kind="fact",
            content="x",
            metadata=[1, 2, 3],  # type: ignore[arg-type]
        )
    async with aiosqlite.connect(bootstrapped_db) as db:
        async with db.execute("SELECT COUNT(*) FROM memories") as cur:
            (count,) = await cur.fetchone()
    assert count == 0


@pytest.mark.parametrize(
    "kind,content,match",
    [
        ("", "hello", "kind must be"),
        ("fact", "", "content must be"),
        (None, "hello", "kind must be"),
        ("fact", 42, "content must be"),
    ],
)
async def test_add_memory_rejects_empty_or_non_string_kind_or_content(
    bootstrapped_db, kind, content, match
):
    """add_memory's write-path validation mirrors
    Memory.__post_init__'s read-path checks — empty / non-str kind or
    content raises ValueError before the DB is opened, so no row is
    written."""
    with pytest.raises(ValueError, match=match):
        await add_memory(bootstrapped_db, kind=kind, content=content)
    async with aiosqlite.connect(bootstrapped_db) as db:
        async with db.execute("SELECT COUNT(*) FROM memories") as cur:
            (count,) = await cur.fetchone()
    assert count == 0


async def test_get_memory_returns_none_on_missing_id(bootstrapped_db):
    """Unknown id → None, not an exception."""
    assert await get_memory(bootstrapped_db, 99999) is None


async def test_get_memory_logs_warning_on_invalid_metadata_json_and_returns_empty_dict(
    bootstrapped_db, caplog
):
    """A corrupt metadata blob (invalid JSON, or valid JSON that isn't
    a dict) logs WARNING and the returned Memory has metadata={}."""
    caplog.set_level(logging.WARNING, logger="agent.memory")

    now_iso = datetime.now(UTC).isoformat()
    async with aiosqlite.connect(bootstrapped_db) as db:
        cursor = await db.execute(
            "INSERT INTO memories (created_at, updated_at, kind, content, metadata) "
            "VALUES (?, ?, ?, ?, ?)",
            (now_iso, now_iso, "fact", "x", "not valid json {{{"),
        )
        await db.commit()
        row_id = cursor.lastrowid

    mem = await get_memory(bootstrapped_db, row_id)
    assert mem is not None
    assert mem.metadata == {}
    assert any(
        f"memory row {row_id} has invalid metadata JSON" in rec.message for rec in caplog.records
    )


async def test_get_memory_logs_warning_on_metadata_json_not_dict_returns_empty(
    bootstrapped_db, caplog
):
    """metadata that parses as JSON but isn't a dict (list, bare str)
    is also treated as corrupt and falls back to empty dict."""
    caplog.set_level(logging.WARNING, logger="agent.memory")
    now_iso = datetime.now(UTC).isoformat()
    async with aiosqlite.connect(bootstrapped_db) as db:
        cursor = await db.execute(
            "INSERT INTO memories (created_at, updated_at, kind, content, metadata) "
            "VALUES (?, ?, ?, ?, ?)",
            (now_iso, now_iso, "fact", "x", json.dumps([1, 2, 3])),
        )
        await db.commit()
        row_id = cursor.lastrowid

    mem = await get_memory(bootstrapped_db, row_id)
    assert mem is not None
    assert mem.metadata == {}
    assert any("invalid metadata JSON" in rec.message for rec in caplog.records)


# -----------------------------------------------------------------
# delete_memory
# -----------------------------------------------------------------


async def test_delete_memory_returns_true_then_false_for_same_id(bootstrapped_db):
    """delete returns True for the existing row; a second delete of
    the same id returns False (no row to remove)."""
    new_id = await add_memory(bootstrapped_db, kind="fact", content="disposable")
    assert await delete_memory(bootstrapped_db, new_id) is True
    assert await delete_memory(bootstrapped_db, new_id) is False
    assert await get_memory(bootstrapped_db, new_id) is None


# -----------------------------------------------------------------
# Memory dataclass validation
# -----------------------------------------------------------------


_GOOD_TS = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "overrides,exc_type",
    [
        # tz-naive timestamps
        ({"created_at": datetime(2026, 1, 1, 12, 0)}, ValueError),
        ({"updated_at": datetime(2026, 1, 1, 12, 0)}, ValueError),
        # empty / wrong-type kind, content
        ({"kind": ""}, ValueError),
        ({"content": ""}, ValueError),
        # id boundaries
        ({"id": 0}, ValueError),
        ({"id": -1}, ValueError),
        # metadata wrong type
        ({"metadata": ["list"]}, TypeError),
    ],
)
def test_memory_dataclass_rejects_invalid_fields(overrides, exc_type):
    """Memory's __post_init__ enforces positive id, tz-aware
    timestamps, non-empty kind/content, and dict metadata. Each
    case names the invalid value in the error message."""
    base = {
        "id": 1,
        "created_at": _GOOD_TS,
        "updated_at": _GOOD_TS,
        "kind": "fact",
        "content": "hello",
        "metadata": {},
    }
    base.update(overrides)
    with pytest.raises(exc_type):
        Memory(**base)


def test_memory_dataclass_accepts_valid_fields():
    """A well-formed Memory constructs without raising."""
    mem = Memory(
        id=1,
        created_at=_GOOD_TS,
        updated_at=_GOOD_TS,
        kind="fact",
        content="hello",
        metadata={"source": "test"},
    )
    assert mem.id == 1
    assert mem.metadata == {"source": "test"}


# -----------------------------------------------------------------
# Concurrency
# -----------------------------------------------------------------


async def test_concurrent_add_memory_from_two_tasks_produces_distinct_ids(bootstrapped_db):
    """Two asyncio tasks adding concurrently receive distinct ids and
    both succeed. aiosqlite serializes DB access on its own thread
    queue, so this is expected to pass without WAL mode in v1."""
    id_a, id_b = await asyncio.gather(
        add_memory(bootstrapped_db, kind="fact", content="a"),
        add_memory(bootstrapped_db, kind="fact", content="b"),
    )
    assert id_a > 0
    assert id_b > 0
    assert id_a != id_b
    assert await get_memory(bootstrapped_db, id_a) is not None
    assert await get_memory(bootstrapped_db, id_b) is not None


# =================================================================
# #77 — FTS5 keyword retrieval
# =================================================================


# -----------------------------------------------------------------
# ensure_fts5_available
# -----------------------------------------------------------------


async def test_ensure_fts5_available_passes_on_fts5_build():
    """The running Python has FTS5 (CI + local dev both do). Probe
    succeeds silently."""
    await ensure_fts5_available()  # must not raise


async def test_ensure_fts5_available_raises_on_missing_fts5(monkeypatch):
    """A SQLite build without FTS5 raises OperationalError with the
    ``no such module: fts5`` marker. ensure_fts5_available translates
    that to FTS5UnavailableError naming the remedy + ADR-0006.

    Monkeypatch ``agent.memory.aiosqlite.connect`` with a fully
    synthetic async context manager whose ``execute`` raises the
    marker — instance-level override on a real aiosqlite connection
    does not stick because ``async with`` looks up ``__aenter__`` on
    the class, not the instance.
    """
    import sqlite3 as _sqlite3

    class _FakeConn:
        async def execute(self, sql, *args, **kwargs):
            raise _sqlite3.OperationalError("no such module: fts5")

    class _FakeCtx:
        async def __aenter__(self):
            return _FakeConn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def fake_connect(*args, **kwargs):
        return _FakeCtx()

    monkeypatch.setattr("agent.memory.aiosqlite.connect", fake_connect)

    with pytest.raises(FTS5UnavailableError, match="FTS5 not available"):
        await ensure_fts5_available()

    # Message surfaces the remedy and the ADR pointer.
    try:
        await ensure_fts5_available()
    except FTS5UnavailableError as exc:
        msg = str(exc)
        assert "python.org" in msg or "pyenv" in msg
        assert "ADR-0006" in msg


# -----------------------------------------------------------------
# v2 migration
# -----------------------------------------------------------------


async def test_bootstrap_db_applies_v3_migration_from_fresh(tmp_path):
    """Fresh DB bootstraps to v3: memories_fts virtual table + three
    triggers exist, schema_version row == 3, and memories_fts is
    created with ``tokenize='porter unicode61'`` so porter stemming
    is active on both stored content and query terms (#109)."""
    path = tmp_path / "memory.db"
    await bootstrap_db(path)

    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT name, type, sql FROM sqlite_master WHERE type IN ('table', 'trigger') "
            "ORDER BY type, name"
        ) as cur:
            items = [(r["name"], r["type"], r["sql"]) for r in await cur.fetchall()]
        async with db.execute("SELECT version FROM schema_version") as cur:
            versions = [r["version"] for r in await cur.fetchall()]

    names = {name for name, _, _ in items}
    triggers = {name for name, typ, _ in items if typ == "trigger"}
    fts_sql = next(sql for name, _, sql in items if name == "memories_fts")
    assert "memories_fts" in names
    assert triggers == {"memories_ai", "memories_ad", "memories_au"}
    assert versions == [SCHEMA_VERSION]
    assert SCHEMA_VERSION == 3
    # Porter stemming must be baked into the virtual table's declared
    # schema — the CREATE VIRTUAL TABLE SQL is the authoritative source.
    assert "porter unicode61" in fts_sql.lower().replace("'", "").replace('"', "")


async def test_bootstrap_db_upgrades_v1_to_latest(tmp_path, monkeypatch):
    """A DB bootstrapped under v1-only (no FTS) then re-bootstrapped
    under the full migration set ends up at SCHEMA_VERSION with the
    porter-stemmed FTS index populated from the pre-existing rows.
    Proves the full v1→v3 upgrade path and that ``'rebuild'`` ran."""
    from agent import memory as memory_mod

    v1_only = [memory_mod._MIGRATIONS[0]]
    monkeypatch.setattr("agent.memory._MIGRATIONS", v1_only)
    monkeypatch.setattr("agent.memory.SCHEMA_VERSION", 1)

    path = tmp_path / "memory.db"
    await bootstrap_db(path)

    # Pre-existing v1 rows — must be findable after upgrade.
    await add_memory(path, kind="fact", content="pumpkin bread recipe")
    await add_memory(path, kind="fact", content="carrot cake frosting")
    await add_memory(path, kind="fact", content="shortbread cookies tin")

    monkeypatch.undo()
    await bootstrap_db(path)

    async with aiosqlite.connect(path) as db:
        async with db.execute("SELECT version FROM schema_version") as cur:
            (version,) = await cur.fetchone()
    assert version == SCHEMA_VERSION
    assert SCHEMA_VERSION == 3

    # rebuild populated the FTS index from existing rows.
    hits = await search_memories(path, "pumpkin")
    assert len(hits) == 1
    assert "pumpkin" in hits[0].content


async def test_bootstrap_db_upgrades_v2_to_v3_with_porter(tmp_path, monkeypatch):
    """A DB bootstrapped under v1+v2 only (unicode61, no stemming) with
    pre-seeded rows upgrades cleanly to v3 (porter unicode61). Post-
    upgrade, (a) the schema_version row is 3, (b) the pre-existing
    rows are still searchable, (c) a query for ``name`` now hits a
    row stored with ``named`` — proving the v3 'rebuild' reindexed
    the base rows under the porter-stemmed tokenizer."""
    from agent import memory as memory_mod

    v1_and_v2 = memory_mod._MIGRATIONS[:2]
    monkeypatch.setattr("agent.memory._MIGRATIONS", v1_and_v2)
    monkeypatch.setattr("agent.memory.SCHEMA_VERSION", 2)

    path = tmp_path / "memory.db"
    await bootstrap_db(path)

    # Pre-existing v2 rows, indexed under unicode61 (no stemming).
    await add_memory(path, kind="fact", content="the user's dog is named Levi")
    await add_memory(path, kind="fact", content="Levi is Ayesha's best friend")

    # Under v2, "name" alone must NOT hit "named" — this guards the
    # pre-upgrade baseline. If this ever fails, the v2 migration grew
    # stemming behind our back and the upgrade test below no longer
    # proves anything new.
    pre_upgrade_hits = await search_memories(path, "name")
    assert pre_upgrade_hits == []

    monkeypatch.undo()
    await bootstrap_db(path)

    async with aiosqlite.connect(path) as db:
        async with db.execute("SELECT version FROM schema_version") as cur:
            (version,) = await cur.fetchone()
    assert version == SCHEMA_VERSION == 3

    # Rows still exist.
    async with aiosqlite.connect(path) as db:
        async with db.execute("SELECT COUNT(*) FROM memories") as cur:
            (count,) = await cur.fetchone()
    assert count == 2

    # Porter stemming is live: "name" hits the "named" row.
    post_upgrade_hits = await search_memories(path, "name")
    assert len(post_upgrade_hits) == 1
    assert "named" in post_upgrade_hits[0].content


async def test_bootstrap_db_v3_is_idempotent_on_up_to_date_db(bootstrapped_db):
    """Bootstrap twice; the v3 migration does not re-run (no duplicate
    triggers, no duplicate virtual tables)."""
    await bootstrap_db(bootstrapped_db)  # second bootstrap, must not raise

    async with aiosqlite.connect(bootstrapped_db) as db:
        async with db.execute(
            "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'trigger')"
        ) as cur:
            items = [(t, n) for t, n in await cur.fetchall()]

    triggers = [n for t, n in items if t == "trigger"]
    fts_tables = [n for t, n in items if t == "table" and n == "memories_fts"]
    assert sorted(triggers) == ["memories_ad", "memories_ai", "memories_au"]
    assert len(fts_tables) == 1


async def test_v3_triggers_keep_fts_in_sync(bootstrapped_db):
    """v3 recreated the three insert/update/delete triggers verbatim
    from v2; this is the regression that proves they survived the
    drop-and-recreate and still keep ``memories_fts`` in sync with
    ``memories`` under porter stemming.

    (a) add_memory fires AFTER INSERT — search finds the row by a
        content term.
    (b) raw UPDATE fires AFTER UPDATE (content changed) — the old
        term misses, the new term hits, and a porter-stemmed variant
        of the new term also hits (end-to-end stemming via trigger).
    (c) delete_memory fires AFTER DELETE — row is gone from the
        index."""
    # (a) insert trigger path.
    new_id = await add_memory(bootstrapped_db, kind="fact", content="apples are crisp")
    hits = await search_memories(bootstrapped_db, "apples")
    assert len(hits) == 1
    assert hits[0].id == new_id

    # (b) update trigger path — old misses, new hits, stem of new hits.
    now_iso = datetime.now(UTC).isoformat()
    async with aiosqlite.connect(bootstrapped_db) as db:
        await db.execute(
            "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
            ("the user is running daily", now_iso, new_id),
        )
        await db.commit()
    assert await search_memories(bootstrapped_db, "apples") == []
    assert len(await search_memories(bootstrapped_db, "running")) == 1
    # Porter-stemmed query — "run" must hit "running" after the update
    # replayed through the porter-tokenized index.
    stem_hits = await search_memories(bootstrapped_db, "run")
    assert len(stem_hits) == 1
    assert stem_hits[0].id == new_id

    # (c) delete trigger path.
    assert await delete_memory(bootstrapped_db, new_id) is True
    assert await search_memories(bootstrapped_db, "running") == []


# -----------------------------------------------------------------
# Triggers: insert / delete / update sync to memories_fts
# -----------------------------------------------------------------


async def test_add_memory_populates_fts_via_insert_trigger(bootstrapped_db):
    """add_memory fires the AFTER INSERT trigger; search immediately
    finds a word from the new row's content."""
    new_id = await add_memory(bootstrapped_db, kind="fact", content="the quick brown fox")
    hits = await search_memories(bootstrapped_db, "fox")
    assert len(hits) == 1
    assert hits[0].id == new_id
    assert hits[0].content == "the quick brown fox"


async def test_delete_memory_removes_from_fts_via_delete_trigger(bootstrapped_db):
    """delete_memory fires the AFTER DELETE trigger (FTS5 'delete'
    command); the row is no longer searchable."""
    new_id = await add_memory(bootstrapped_db, kind="fact", content="uniqueterm123 temporary")
    assert len(await search_memories(bootstrapped_db, "uniqueterm123")) == 1
    assert await delete_memory(bootstrapped_db, new_id) is True
    assert await search_memories(bootstrapped_db, "uniqueterm123") == []


async def test_update_trigger_refreshes_fts(bootstrapped_db):
    """A raw UPDATE on memories.content fires the content-change
    AFTER UPDATE trigger: searching for the old content misses,
    searching for the new content hits. Exercises the trigger
    directly — no public update_memory yet."""
    new_id = await add_memory(bootstrapped_db, kind="fact", content="originalalpha before")
    now_iso = datetime.now(UTC).isoformat()
    async with aiosqlite.connect(bootstrapped_db) as db:
        await db.execute(
            "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
            ("replacedbeta after", now_iso, new_id),
        )
        await db.commit()

    assert await search_memories(bootstrapped_db, "originalalpha") == []
    new_hits = await search_memories(bootstrapped_db, "replacedbeta")
    assert len(new_hits) == 1
    assert new_hits[0].id == new_id


# -----------------------------------------------------------------
# search_memories
# -----------------------------------------------------------------


async def test_search_memories_returns_empty_list_on_empty_db(bootstrapped_db):
    """No memories → search returns []."""
    assert await search_memories(bootstrapped_db, "anything") == []


async def test_search_memories_normalizes_operator_keywords(bootstrapped_db):
    """Operator keywords AND/OR are stripped; the remaining terms
    are OR'd and ranked by bm25, and a row that mentions any of them
    is returned (#109)."""
    await add_memory(
        bootstrapped_db,
        kind="fact",
        content="a cup of coffee, some tea, and warm milk",
    )
    hits = await search_memories(bootstrapped_db, "coffee AND tea OR milk")
    assert len(hits) == 1


async def test_search_memories_normalizes_operator_punctuation(bootstrapped_db):
    """Operator punctuation (``* " ( ) : - ^``) is stripped; the
    remaining bare words find a matching row under implicit-AND."""
    await add_memory(bootstrapped_db, kind="fact", content="espresso latte mocha")
    hits = await search_memories(bootstrapped_db, '"espresso" (latte) -mocha')
    assert len(hits) == 1


async def test_search_memories_returns_empty_on_pure_noise_query(bootstrapped_db, monkeypatch):
    """A query consisting entirely of operator punctuation normalizes
    to empty; search short-circuits with [] WITHOUT opening the DB."""
    real_connect = aiosqlite.connect
    connect_called = False

    def tracking_connect(*args, **kwargs):
        nonlocal connect_called
        connect_called = True
        return real_connect(*args, **kwargs)

    monkeypatch.setattr("agent.memory.aiosqlite.connect", tracking_connect)

    result = await search_memories(bootstrapped_db, '"":()*^')
    assert result == []
    assert connect_called is False


async def test_search_memories_ranks_by_relevance(bootstrapped_db):
    """FTS5's bm25 rank orders higher term-density rows first."""
    await add_memory(bootstrapped_db, kind="fact", content="one coffee")
    await add_memory(bootstrapped_db, kind="fact", content="coffee coffee tea")
    await add_memory(bootstrapped_db, kind="fact", content="coffee coffee coffee")

    hits = await search_memories(bootstrapped_db, "coffee", k=3)
    contents = [h.content for h in hits]
    assert contents == [
        "coffee coffee coffee",
        "coffee coffee tea",
        "one coffee",
    ]


async def test_search_memories_respects_k_limit(bootstrapped_db):
    """k caps the result length even when more rows match."""
    for i in range(10):
        await add_memory(bootstrapped_db, kind="fact", content=f"match number {i}")
    hits = await search_memories(bootstrapped_db, "match", k=3)
    assert len(hits) == 3


@pytest.mark.parametrize(
    "bad_k",
    [0, -1, 1.5, "3", True, None],
)
async def test_search_memories_rejects_k_le_zero(bootstrapped_db, bad_k):
    """k ≤ 0, non-int types, and bool (despite isinstance(True, int))
    all raise ValueError before any work."""
    with pytest.raises(ValueError, match="k must be a positive int"):
        await search_memories(bootstrapped_db, "anything", k=bad_k)


async def test_search_memories_preserves_non_ascii(bootstrapped_db):
    """FTS5 tokenizer handles UTF-8; a non-ASCII query round-trips to
    a row with that same content."""
    await add_memory(bootstrapped_db, kind="fact", content="I love café in the morning")
    hits = await search_memories(bootstrapped_db, "café")
    assert len(hits) == 1
    assert "café" in hits[0].content


async def test_search_memories_logs_warning_on_corrupt_metadata_in_hit(bootstrapped_db, caplog):
    """A search hit with a corrupt metadata blob triggers the same
    warn+fallback path as get_memory (via _row_to_memory). The
    returned Memory has metadata={} and the WARNING log names the
    row id."""
    caplog.set_level(logging.WARNING, logger="agent.memory")
    now_iso = datetime.now(UTC).isoformat()
    async with aiosqlite.connect(bootstrapped_db) as db:
        cursor = await db.execute(
            "INSERT INTO memories (created_at, updated_at, kind, content, metadata) "
            "VALUES (?, ?, ?, ?, ?)",
            (now_iso, now_iso, "fact", "findmearzel dolphin", "not valid json {{{"),
        )
        await db.commit()
        row_id = cursor.lastrowid

    hits = await search_memories(bootstrapped_db, "findmearzel")
    assert len(hits) == 1
    assert hits[0].id == row_id
    assert hits[0].metadata == {}
    assert any(
        f"memory row {row_id} has invalid metadata JSON" in rec.message for rec in caplog.records
    )


async def test_search_memories_whole_word_operator_stripping(bootstrapped_db):
    """The ``\\b(AND|OR|NOT|NEAR)\\b`` whole-word regex must NOT
    strip ``ANDROID`` down to ``DROID``. If this ever fails, the
    normalization regex regressed to substring stripping."""
    await add_memory(bootstrapped_db, kind="fact", content="ANDROID sales up")
    hits = await search_memories(bootstrapped_db, "ANDROID sales")
    assert len(hits) == 1
    assert "ANDROID" in hits[0].content


async def test_search_memories_or_semantics_returns_partial_matches(bootstrapped_db):
    """OR semantics (#109): a query term that matches a row returns
    that row even when the row misses other query terms. Reverses the
    implicit-AND decision locked by #77 / PR #100 / PR #103."""
    await add_memory(bootstrapped_db, kind="fact", content="coffee and tea")
    hits = await search_memories(bootstrapped_db, "coffee elephant")
    assert len(hits) == 1
    assert "coffee" in hits[0].content


async def test_search_memories_handles_conversational_punctuation(bootstrapped_db):
    """FTS5 rejects bare ``,`` ``'`` ``?`` ``.`` in MATCH expressions,
    and the unicode61 tokenizer treats ``,`` as token glue creating
    phantom adjacency requirements. The ``[\\W_]+`` strip handles both
    — a typical conversational query round-trips cleanly to a bareword
    MATCH with no parse error. Under #109's OR semantics, any of the
    content words is enough to hit the seed row; this test asserts
    the parse-safety invariant, not the ranking contract."""
    await add_memory(
        bootstrapped_db,
        kind="fact",
        content="what's the meeting with Sarah tomorrow at noon",
    )
    hits = await search_memories(
        bootstrapped_db,
        "what's the meeting with Sarah, tomorrow?",
    )
    assert len(hits) == 1
    assert "Sarah" in hits[0].content
    assert "tomorrow" in hits[0].content


async def test_search_memories_strips_underscores(bootstrapped_db):
    """The unicode61 tokenizer treats ``_`` as a separator; the
    normalization strips it before MATCH so a query like
    ``machine_learning`` cleanly matches a memory whose content is
    ``machine learning fundamentals``."""
    await add_memory(
        bootstrapped_db,
        kind="fact",
        content="machine learning fundamentals",
    )
    hits = await search_memories(bootstrapped_db, "machine_learning fundamentals")
    assert len(hits) == 1


async def test_search_memories_normalizes_underscore_glued_operator(bootstrapped_db):
    """Underscore-glued operator keywords (e.g. ``coffee_OR_tea``) are
    stripped after the non-word strip exposes them. Under #109's OR
    semantics, ``coffee_OR_tea`` normalizes to terms ``coffee`` and
    ``tea``, OR'd — so a row mentioning either hits. This test proves
    the keyword did NOT leak into MATCH as an FTS5 operator (that
    would be an unquoted ``OR``, which would still be fine under OR
    semantics but changes the parse — the real regression here is
    that the ``_OR_`` glue doesn't survive)."""
    await add_memory(bootstrapped_db, kind="fact", content="only coffee here")
    hits = await search_memories(bootstrapped_db, "coffee_OR_tea")
    assert len(hits) == 1
    assert hits[0].content == "only coffee here"

    await add_memory(bootstrapped_db, kind="fact", content="only tea here")
    hits = await search_memories(bootstrapped_db, "coffee_OR_tea", k=5)
    contents = sorted(h.content for h in hits)
    assert contents == ["only coffee here", "only tea here"]


# -----------------------------------------------------------------
# #109: OR + stopwords + porter retrieval
# -----------------------------------------------------------------


async def test_search_memories_stopwords_only_short_circuits_without_db_access(
    bootstrapped_db, monkeypatch
):
    """A query that is nothing but stopwords (e.g. ``what is my``)
    normalizes to zero terms and returns [] without opening the DB.
    Same connect-tracking pattern as the pure-noise-query test
    (#109)."""
    real_connect = aiosqlite.connect
    connect_called = False

    def tracking_connect(*args, **kwargs):
        nonlocal connect_called
        connect_called = True
        return real_connect(*args, **kwargs)

    monkeypatch.setattr("agent.memory.aiosqlite.connect", tracking_connect)

    result = await search_memories(bootstrapped_db, "what is my")
    assert result == []
    assert connect_called is False


async def test_search_memories_stopword_dropping_recovers_natural_language(bootstrapped_db):
    """A conversational query like ``tell me about levi`` must return
    the ``Levi`` row. Under the pre-#109 implicit-AND semantics every
    term (``tell``, ``me``, ``about``, ``levi``) had to appear and no
    row did. Under #109 the stopword list drops the fillers and the
    remaining single term hits (#109 AC bullet 2)."""
    await add_memory(bootstrapped_db, kind="fact", content="The user's dog is named Levi.")
    hits = await search_memories(bootstrapped_db, "tell me about levi")
    assert len(hits) == 1
    assert "Levi" in hits[0].content


async def test_search_memories_porter_stems_name_to_named(bootstrapped_db):
    """Porter stemming (schema v3) maps ``name`` ↔ ``named``. A query
    for ``dog name`` hits a row stored with ``named``. This is the
    other half of the #109 fix: without porter, ``name`` and
    ``named`` are distinct tokens in unicode61 and the query misses
    (#109 AC bullet 1 / bullet 3)."""
    await add_memory(bootstrapped_db, kind="fact", content="The user's dog is named Levi.")
    hits = await search_memories(bootstrapped_db, "dog name")
    assert len(hits) == 1
    assert "named" in hits[0].content


async def test_search_memories_bm25_prefers_multi_term_matches(bootstrapped_db):
    """bm25 ranks a row matching N query terms above a row matching
    M<N query terms under OR semantics. This is the precision guard
    for #109 — without it, a single-term match against a common word
    could outrank a multi-term match.

    Seeds three rows: one has both terms (dog + name), one has only
    ``dog``, one has only ``name``. Query ``dog name`` must return
    the both-terms row first; the one-term rows follow (if at all)."""
    both_id = await add_memory(
        bootstrapped_db,
        kind="fact",
        content="the user's dog is named Levi",
    )
    await add_memory(bootstrapped_db, kind="fact", content="the user has a dog")
    await add_memory(bootstrapped_db, kind="fact", content="the project is named HelloWorld")

    hits = await search_memories(bootstrapped_db, "dog name", k=5)
    assert len(hits) >= 1
    assert hits[0].id == both_id


async def test_search_memories_operator_keywords_cannot_leak_into_match_quoted(bootstrapped_db):
    """Regression: a user typing literal FTS5 operator syntax must
    not alter query semantics or raise a parse error. Terms are
    double-quoted before building MATCH, so any surviving operator
    keyword is a phrase token (not an operator)."""
    await add_memory(bootstrapped_db, kind="fact", content="coffee and tea together")
    # Punctuation strip first produces ``OR with AND``; operator-keyword
    # strip produces `` with ``; stopword strip produces []. Short-
    # circuit to [] — no FTS5 parse error either way.
    assert await search_memories(bootstrapped_db, '"OR" with (AND)') == []


async def test_search_memories_109_e2e_scenarios(bootstrapped_db):
    """The three E2E queries from the 2026-11-13 manual run (#109
    acceptance criteria), end-to-end against the real porter+OR
    pipeline and a DB seeded with the exact two E2E rows.

    AC 1: ``what's my dog's name`` ranks the dog fact first.
    AC 2: ``tell me about levi`` returns both rows.
    AC 3: ``dog name`` returns the dog fact.
    C1:   ``tell me about levi`` reduces to exactly ['levi'] after
          stopword filtering (asserted via the # of returned rows —
          both rows mention Levi only). If the stopword list regresses
          and ``about`` or ``tell`` survives, neither row would match
          AND the assertion would fail."""
    dog_id = await add_memory(
        bootstrapped_db,
        kind="fact",
        content="The user's dog is named Levi.",
    )
    friend_id = await add_memory(
        bootstrapped_db,
        kind="fact",
        content="Levi is Ayesha's best friend.",
    )

    # AC 1: dog's name ranks the dog fact first.
    hits = await search_memories(bootstrapped_db, "what's my dog's name")
    assert len(hits) >= 1
    assert hits[0].id == dog_id

    # AC 2: tell me about levi returns both rows (any order).
    hits = await search_memories(bootstrapped_db, "tell me about levi", k=5)
    ids = {h.id for h in hits}
    assert ids == {dog_id, friend_id}

    # AC 3: dog name returns the dog fact as the top hit (porter
    # stems ``name`` to the stored ``named``).
    hits = await search_memories(bootstrapped_db, "dog name")
    assert len(hits) >= 1
    assert hits[0].id == dog_id


# =================================================================
# #80 — list_memories (ADR-0006 §Public surface, deferred from #76)
# =================================================================


async def test_list_memories_empty_db_returns_empty_list(bootstrapped_db):
    """No rows → []."""
    assert await list_memories(bootstrapped_db) == []


async def test_list_memories_returns_all_in_created_at_desc_order(bootstrapped_db, monkeypatch):
    """Rows come back newest-first by ``created_at`` DESC. Timestamps
    are seeded explicitly via raw INSERT so ordering is deterministic
    without relying on inter-call sleep."""
    base = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    async with aiosqlite.connect(bootstrapped_db) as db:
        for i in range(5):
            ts = (base + timedelta(minutes=i)).isoformat()
            await db.execute(
                "INSERT INTO memories (created_at, updated_at, kind, content, metadata) "
                "VALUES (?, ?, ?, ?, ?)",
                (ts, ts, "fact", f"row {i}", "{}"),
            )
        await db.commit()

    results = await list_memories(bootstrapped_db)
    assert [m.content for m in results] == [
        "row 4",
        "row 3",
        "row 2",
        "row 1",
        "row 0",
    ]


async def test_list_memories_respects_limit(bootstrapped_db):
    """``limit`` caps the result length even when more rows exist."""
    for i in range(10):
        await add_memory(bootstrapped_db, kind="fact", content=f"row {i}")
    results = await list_memories(bootstrapped_db, limit=3)
    assert len(results) == 3


async def test_list_memories_filters_by_kind(bootstrapped_db):
    """``kind=X`` filters to exact-match rows only."""
    await add_memory(bootstrapped_db, kind="fact", content="a fact")
    await add_memory(bootstrapped_db, kind="preference", content="a pref")
    await add_memory(bootstrapped_db, kind="fact", content="another fact")

    only_prefs = await list_memories(bootstrapped_db, kind="preference")
    assert len(only_prefs) == 1
    assert only_prefs[0].kind == "preference"

    only_facts = await list_memories(bootstrapped_db, kind="fact")
    assert len(only_facts) == 2
    assert all(m.kind == "fact" for m in only_facts)


async def test_list_memories_filter_by_kind_with_no_matches_returns_empty(bootstrapped_db):
    """A ``kind`` value that matches nothing in the DB → []."""
    await add_memory(bootstrapped_db, kind="fact", content="only facts here")
    assert await list_memories(bootstrapped_db, kind="person") == []


@pytest.mark.parametrize("bad_limit", [0, -1, 1.5, "3", True, None])
async def test_list_memories_rejects_non_positive_limit(bootstrapped_db, bad_limit):
    """``limit`` must be a positive ``int`` — bool rejected despite
    being an int subclass, mirroring ``search_memories``'s posture."""
    with pytest.raises(ValueError, match="limit must be a positive int"):
        await list_memories(bootstrapped_db, limit=bad_limit)


@pytest.mark.parametrize("bad_kind", [123, [], {}, 1.5])
async def test_list_memories_rejects_non_string_kind(bootstrapped_db, bad_kind):
    """``kind`` must be ``str`` or ``None`` — anything else raises
    ValueError before any DB work."""
    with pytest.raises(ValueError, match="kind must be a str or None"):
        await list_memories(bootstrapped_db, kind=bad_kind)


# =================================================================
# #78 — Memory writer (explicit cues + classifier)
# =================================================================


# -----------------------------------------------------------------
# detect_explicit_cue
# -----------------------------------------------------------------


def test_detect_explicit_cue_matches_remember_that():
    """`remember that X` extracts X, kind=fact, cue_type=remember_that."""
    from agent.memory import detect_explicit_cue

    assert detect_explicit_cue("please remember that I like oat milk") == (
        "fact",
        "I like oat milk",
        "remember_that",
    )


def test_detect_explicit_cue_matches_take_note():
    """`take a note that X` extracts X, cue_type=take_a_note."""
    from agent.memory import detect_explicit_cue

    assert detect_explicit_cue("take a note that my manager's name is Ana") == (
        "fact",
        "my manager's name is Ana",
        "take_a_note",
    )


def test_detect_explicit_cue_matches_dont_forget():
    """Both `don't forget X` and `do not forget that X` match with
    cue_type=dont_forget."""
    from agent.memory import detect_explicit_cue

    assert detect_explicit_cue("don't forget my dentist is Dr. Lee") == (
        "fact",
        "my dentist is Dr. Lee",
        "dont_forget",
    )
    assert detect_explicit_cue("do not forget that the deadline is Friday") == (
        "fact",
        "the deadline is Friday",
        "dont_forget",
    )


def test_detect_explicit_cue_case_insensitive():
    """All-caps cues still match (voice-STT may uppercase proper nouns
    or the whole utterance)."""
    from agent.memory import detect_explicit_cue

    result = detect_explicit_cue("REMEMBER THAT I use pyenv")
    assert result is not None
    assert result[0] == "fact"
    assert result[1] == "I use pyenv"
    assert result[2] == "remember_that"


def test_detect_explicit_cue_no_match_on_absent_cue():
    """Non-cue utterances return None."""
    from agent.memory import detect_explicit_cue

    assert detect_explicit_cue("I had coffee today") is None


def test_detect_explicit_cue_no_match_on_cue_without_content():
    """A cue followed by nothing (or just punctuation) is not a write
    signal — return None rather than persist an empty content row."""
    from agent.memory import detect_explicit_cue

    assert detect_explicit_cue("remember that.") is None
    assert detect_explicit_cue("remember that") is None


def test_detect_explicit_cue_strips_trailing_punctuation():
    """Sentence-terminating punctuation on the user turn's end is
    stripped from the extracted content."""
    from agent.memory import detect_explicit_cue

    assert detect_explicit_cue("remember that I like espresso!") == (
        "fact",
        "I like espresso",
        "remember_that",
    )


def test_detect_explicit_cue_known_edge_case_whitespace_prefix_matches():
    """V1 known imperfection: whitespace-prefixed subordinate-clause
    contexts DO match. Lock the current behavior in so a future
    tightening (discourse-marker allowlist) surfaces as a visible
    test change."""
    from agent.memory import detect_explicit_cue

    result = detect_explicit_cue("I had to remember that time")
    assert result is not None
    assert result[2] == "remember_that"
    assert result[1] == "time"


# -----------------------------------------------------------------
# schedule_memory_classification
# -----------------------------------------------------------------


async def test_schedule_memory_classification_writes_on_true(bootstrapped_db, monkeypatch):
    """When the classifier returns a MemoryClassification, the task
    writes it to the DB via add_memory."""
    from agent.claude import MemoryClassification
    from agent.memory import schedule_memory_classification

    async def fake_classify(user_turn, prior_assistant=None):
        return MemoryClassification(kind="fact", content="hi there", metadata={})

    monkeypatch.setattr("agent.memory.classify_turn_for_memory", fake_classify)

    registry: set = set()
    task = schedule_memory_classification(bootstrapped_db, "anything", task_registry=registry)
    await task

    async with aiosqlite.connect(bootstrapped_db) as db:
        async with db.execute("SELECT COUNT(*) FROM memories") as cur:
            (count,) = await cur.fetchone()
    assert count == 1


async def test_schedule_memory_classification_no_write_on_none(bootstrapped_db, monkeypatch):
    """When the classifier returns None, no row is written."""
    from agent.memory import schedule_memory_classification

    async def fake_classify(user_turn, prior_assistant=None):
        return None

    monkeypatch.setattr("agent.memory.classify_turn_for_memory", fake_classify)

    registry: set = set()
    task = schedule_memory_classification(
        bootstrapped_db, "nothing durable", task_registry=registry
    )
    await task

    async with aiosqlite.connect(bootstrapped_db) as db:
        async with db.execute("SELECT COUNT(*) FROM memories") as cur:
            (count,) = await cur.fetchone()
    assert count == 0


async def test_schedule_memory_classification_swallows_classifier_exceptions(
    bootstrapped_db, monkeypatch
):
    """Classifier raising doesn't leak into the event loop.
    task.exception() is None after await (the body swallowed it)."""
    from agent.memory import schedule_memory_classification

    async def exploding_classify(user_turn, prior_assistant=None):
        raise RuntimeError("simulated classifier failure")

    monkeypatch.setattr("agent.memory.classify_turn_for_memory", exploding_classify)

    registry: set = set()
    task = schedule_memory_classification(bootstrapped_db, "anything", task_registry=registry)
    await task
    assert task.exception() is None


async def test_schedule_memory_classification_retains_task_reference(bootstrapped_db, monkeypatch):
    """The spawned task is in the registry BEFORE awaiting (strong
    reference prevents asyncio GC) and discarded AFTER awaiting (the
    done-callback runs)."""
    from agent.memory import schedule_memory_classification

    async def slow_classify(user_turn, prior_assistant=None):
        # Give the test a window to observe membership before done.
        await asyncio.sleep(0)
        return None

    monkeypatch.setattr("agent.memory.classify_turn_for_memory", slow_classify)

    registry: set = set()
    task = schedule_memory_classification(bootstrapped_db, "anything", task_registry=registry)
    assert task in registry
    await task
    assert task not in registry


# -----------------------------------------------------------------
# process_turn_for_memory
# -----------------------------------------------------------------


async def test_process_turn_for_memory_explicit_cue_writes_synchronously(
    bootstrapped_db, monkeypatch
):
    """An explicit cue writes within the same await — the row exists
    BEFORE the classifier task is awaited."""
    from agent.memory import process_turn_for_memory

    async def fake_classify(user_turn, prior_assistant=None):
        # Would spawn a second write; keep this test focused on the
        # synchronous path.
        return None

    monkeypatch.setattr("agent.memory.classify_turn_for_memory", fake_classify)

    registry: set = set()
    explicit_id, task = await process_turn_for_memory(
        bootstrapped_db,
        "please remember that I like oat milk",
        task_registry=registry,
    )

    # Before awaiting the classifier task: explicit row already exists.
    assert isinstance(explicit_id, int) and explicit_id > 0
    async with aiosqlite.connect(bootstrapped_db) as db:
        async with db.execute("SELECT COUNT(*) FROM memories") as cur:
            (count,) = await cur.fetchone()
    assert count == 1

    # Clean up the pending task so pytest doesn't warn.
    await task


async def test_process_turn_for_memory_also_spawns_classifier_on_cue(bootstrapped_db, monkeypatch):
    """Both paths fire on a cue-containing turn: one explicit row +
    one classifier row = two rows after awaiting the task."""
    from agent.claude import MemoryClassification
    from agent.memory import process_turn_for_memory

    async def fake_classify(user_turn, prior_assistant=None):
        return MemoryClassification(
            kind="preference",
            content="prefers oat milk (classifier)",
            metadata={},
        )

    monkeypatch.setattr("agent.memory.classify_turn_for_memory", fake_classify)

    registry: set = set()
    explicit_id, task = await process_turn_for_memory(
        bootstrapped_db,
        "please remember that I like oat milk",
        task_registry=registry,
    )
    await task

    assert isinstance(explicit_id, int) and explicit_id > 0
    async with aiosqlite.connect(bootstrapped_db) as db:
        async with db.execute("SELECT content, metadata FROM memories ORDER BY id") as cur:
            rows = [tuple(r) for r in await cur.fetchall()]
    assert len(rows) == 2
    # First row = explicit cue (source=explicit_cue).
    assert '"source": "explicit_cue"' in rows[0][1]
    # Second row = classifier (source=classifier).
    assert '"source": "classifier"' in rows[1][1]


async def test_process_turn_for_memory_classifier_only_on_non_cue(bootstrapped_db, monkeypatch):
    """A turn with no cue returns explicit_id=None but still spawns
    the classifier. After awaiting, there's exactly one row (the
    classifier's)."""
    from agent.claude import MemoryClassification
    from agent.memory import process_turn_for_memory

    async def fake_classify(user_turn, prior_assistant=None):
        return MemoryClassification(
            kind="fact",
            content="user had coffee today (classifier)",
            metadata={},
        )

    monkeypatch.setattr("agent.memory.classify_turn_for_memory", fake_classify)

    registry: set = set()
    explicit_id, task = await process_turn_for_memory(
        bootstrapped_db,
        "I had coffee today",
        task_registry=registry,
    )
    await task

    assert explicit_id is None
    async with aiosqlite.connect(bootstrapped_db) as db:
        async with db.execute("SELECT content, metadata FROM memories") as cur:
            rows = [tuple(r) for r in await cur.fetchall()]
    assert len(rows) == 1
    assert '"source": "classifier"' in rows[0][1]


def test_process_turn_for_memory_subprocess_integration(tmp_path):
    """End-to-end subprocess test: spawn python, bootstrap a DB,
    call process_turn_for_memory with a cue turn, exit cleanly,
    then assert the explicit-cue row persisted to disk. The
    classifier is mocked inside the subprocess so the test does
    NOT hit the Anthropic API.
    """
    import subprocess
    import sys
    import textwrap

    REPO_ROOT = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "memory.db"

    script = textwrap.dedent(
        f"""
        import asyncio
        import sys
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from pathlib import Path
        from agent.memory import bootstrap_db, process_turn_for_memory

        # Mock the classifier so we don't call Anthropic.
        async def _fake_classify(user_turn, prior_assistant=None):
            return None

        import agent.memory as mem
        mem.classify_turn_for_memory = _fake_classify

        async def main():
            db = Path({str(db_path)!r})
            await bootstrap_db(db)
            registry = set()
            _explicit_id, task = await process_turn_for_memory(
                db,
                "remember that FRIDAY uses SQLite for long-term memory",
                task_registry=registry,
            )
            await task

        asyncio.run(main())
        """
    ).strip()

    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"subprocess failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    # Parent reads the DB and asserts the explicit-cue row persisted.
    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(db_path)
    try:
        rows = list(conn.execute("SELECT kind, content, metadata FROM memories ORDER BY id"))
    finally:
        conn.close()

    assert len(rows) == 1
    kind, content, metadata_json = rows[0]
    assert kind == "fact"
    assert content == "FRIDAY uses SQLite for long-term memory"
    assert '"source": "explicit_cue"' in metadata_json
    assert '"cue_type": "remember_that"' in metadata_json
