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
    search_memories,
)


@pytest.fixture
async def bootstrapped_db(tmp_path: Path) -> Path:
    """A ready-to-use memory.db with v1 schema in place."""
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


async def test_bootstrap_db_applies_v2_migration_from_fresh(tmp_path):
    """Fresh DB bootstraps to v2: memories_fts virtual table + three
    triggers exist, schema_version row == 2."""
    path = tmp_path / "memory.db"
    await bootstrap_db(path)

    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'trigger') "
            "ORDER BY type, name"
        ) as cur:
            items = [(r["name"], r["type"]) for r in await cur.fetchall()]
        async with db.execute("SELECT version FROM schema_version") as cur:
            versions = [r["version"] for r in await cur.fetchall()]

    names = {name for name, _ in items}
    triggers = {name for name, typ in items if typ == "trigger"}
    assert "memories_fts" in names
    assert triggers == {"memories_ai", "memories_ad", "memories_au"}
    assert versions == [SCHEMA_VERSION]
    assert SCHEMA_VERSION == 2


async def test_bootstrap_db_upgrades_v1_to_v2(tmp_path, monkeypatch):
    """A DB bootstrapped under v1-only (no FTS) then re-bootstrapped
    under the real v1+v2 migrations ends up at v2 with the FTS index
    populated from the pre-existing rows. Proves ``'rebuild'`` ran."""
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
    assert SCHEMA_VERSION == 2

    # rebuild populated the FTS index from existing rows.
    hits = await search_memories(path, "pumpkin")
    assert len(hits) == 1
    assert "pumpkin" in hits[0].content


async def test_bootstrap_db_v2_is_idempotent_on_up_to_date_db(bootstrapped_db):
    """Bootstrap twice; v2 migration does not re-run (no duplicate
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
    find a row that contains all of them under implicit-AND."""
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


async def test_search_memories_implicit_and_requires_all_terms(bootstrapped_db):
    """Implicit-AND MATCH: every term in the bag must appear in the
    row. One missing term → no hit. If MATCH ever drifts to OR
    semantics, this test catches it."""
    await add_memory(bootstrapped_db, kind="fact", content="coffee and tea")
    hits = await search_memories(bootstrapped_db, "coffee elephant")
    assert hits == []


async def test_search_memories_handles_conversational_punctuation(bootstrapped_db):
    """FTS5 rejects bare ``,`` ``'`` ``?`` ``.`` in MATCH expressions,
    and the unicode61 tokenizer treats ``,`` as token glue creating
    phantom adjacency requirements. The ``[\\W_]+`` strip handles both
    — a typical conversational query round-trips cleanly to a bareword
    MATCH with no parse error. Seed content mirrors the conversational
    query's word bag so the implicit-AND invariant (every token must
    appear) is satisfied — the fix is about avoiding the FTS5 parse
    error and avoiding phantom adjacency from comma-glue, not about
    relaxing the implicit-AND semantics."""
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
    stripped after the non-word strip exposes them, keeping implicit-AND
    semantics. Previously, the operator-keyword strip ran first with
    ``\\b`` boundaries that didn't match ``_OR_``, then the underscore
    strip exposed ``OR`` to FTS5 as an operator — leaking OR semantics
    into what the ADR promises as implicit-AND."""
    await add_memory(bootstrapped_db, kind="fact", content="only coffee here")
    # Pre-fix: this query returned the row (OR semantics, matched "coffee").
    # Post-fix: "coffee_OR_tea" normalizes to "coffee tea" and the seed row
    # doesn't contain "tea", so implicit-AND correctly rejects it.
    hits = await search_memories(bootstrapped_db, "coffee_OR_tea")
    assert hits == []

    # Positive control: a seed that contains BOTH terms matches after
    # the operator-keyword strip runs on the exposed ``OR``.
    await add_memory(bootstrapped_db, kind="fact", content="coffee and tea together")
    hits = await search_memories(bootstrapped_db, "coffee_OR_tea")
    assert len(hits) == 1
    assert hits[0].content == "coffee and tea together"
