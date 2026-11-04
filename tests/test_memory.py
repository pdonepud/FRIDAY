"""Tests for :mod:`agent.memory` (ADR-0006 §Long-term SQLite scaffolding)."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from agent.memory import (
    SCHEMA_VERSION,
    Memory,
    MemoryDBError,
    add_memory,
    bootstrap_db,
    delete_memory,
    get_memory,
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
