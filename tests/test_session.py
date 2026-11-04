"""Tests for :mod:`agent.session` (ADR-0006 §Short-term persistence)."""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent import session
from agent.buffer import ConversationBuffer, Turn
from agent.session import (
    SCHEMA_VERSION,
    SESSION_FILENAME,
    _run_save,
    default_session_path,
    load_session,
    register_shutdown_save,
    save_session,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset_registered():
    """Clear agent.session._registered between tests so idempotency
    guards from one test don't leak into another."""
    session._registered.clear()
    yield
    session._registered.clear()


# -----------------------------------------------------------------
# default_session_path
# -----------------------------------------------------------------


def test_default_session_path_ends_with_session_json(monkeypatch, tmp_path):
    """default_session_path() returns <data_dir>/session.json."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert default_session_path() == tmp_path / "friday" / SESSION_FILENAME


# -----------------------------------------------------------------
# save_session — round-trip + encoding + atomicity
# -----------------------------------------------------------------


def test_save_session_round_trips_empty_buffer(tmp_path):
    """Empty buffer serializes to {"version": 1, "turns": []}."""
    path = tmp_path / "session.json"
    save_session(ConversationBuffer(turn_limit=5), path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"version": SCHEMA_VERSION, "turns": []}


def test_save_session_round_trips_populated_buffer(tmp_path):
    """A populated buffer saves then loads back to the same turns."""
    path = tmp_path / "session.json"
    buf = ConversationBuffer(turn_limit=5)
    buf.append(Turn(role="user", content="hi"))
    buf.append(Turn(role="assistant", content="hello"))
    save_session(buf, path)

    loaded = load_session(path, turn_limit=5)
    assert loaded.as_messages() == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def test_save_session_preserves_tz_aware_timestamps(tmp_path):
    """datetime.isoformat() / fromisoformat() preserves the tz offset."""
    path = tmp_path / "session.json"
    when = datetime(2026, 11, 3, 14, 2, 11, 123456, tzinfo=UTC)
    buf = ConversationBuffer(turn_limit=5)
    buf.append(Turn(role="user", content="hi", timestamp=when))
    save_session(buf, path)

    loaded = load_session(path, turn_limit=5)
    (only,) = list(loaded)
    assert only.timestamp == when
    assert only.timestamp.tzinfo is not None


def test_save_session_preserves_non_ascii_and_newlines(tmp_path):
    """ensure_ascii=False keeps UTF-8; content with emoji + newlines
    round-trips byte-for-byte."""
    path = tmp_path / "session.json"
    weird = "héllo 🌊\n  nested  line\twith tabs — em dashes too"
    buf = ConversationBuffer(turn_limit=5)
    buf.append(Turn(role="user", content=weird))
    save_session(buf, path)

    loaded = load_session(path, turn_limit=5)
    (only,) = list(loaded)
    assert only.content == weird


def test_save_session_writes_atomically_via_temp(monkeypatch, tmp_path):
    """os.replace fires exactly once, with src a .session.*.tmp in the
    target's parent and dst the target path."""
    path = tmp_path / "session.json"
    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(src, dst, **kwargs):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", spy)
    monkeypatch.setattr(session.os, "replace", spy)

    buf = ConversationBuffer(turn_limit=5)
    buf.append(Turn(role="user", content="atomic"))
    save_session(buf, path)

    assert len(calls) == 1
    src, dst = calls[0]
    assert dst == str(path)
    assert Path(src).parent == tmp_path
    assert Path(src).name.startswith(".session.")
    assert Path(src).name.endswith(".tmp")


def test_save_session_cleans_up_temp_on_write_failure(monkeypatch, tmp_path, caplog):
    """A failure between temp write and os.replace must unlink the
    temp file and leave the target untouched."""
    caplog.set_level(logging.WARNING, logger="agent.session")
    path = tmp_path / "session.json"

    def boom(src, dst, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(session.os, "replace", boom)

    buf = ConversationBuffer(turn_limit=5)
    buf.append(Turn(role="user", content="should not land"))
    save_session(buf, path)

    assert not path.exists()
    # No stray .session.*.tmp in the parent.
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".session.")]
    assert leftovers == []
    # Warning surfaced.
    assert any("session save failed" in rec.message for rec in caplog.records)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only file-mode bits")
def test_save_session_file_mode_is_0o600_on_posix(tmp_path):
    """The written file is 0o600 (user-only r/w) on POSIX."""
    path = tmp_path / "session.json"
    save_session(ConversationBuffer(turn_limit=5), path)
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_save_session_logs_warning_on_unicode_error_no_raise(tmp_path, caplog):
    """#75 CR F1: a Turn containing a lone surrogate (non-UTF-8-encodable
    code point) in its content must not escape ``save_session`` as a
    ``UnicodeEncodeError`` from json.dump — the save path is called
    from atexit and must never raise."""
    caplog.set_level(logging.WARNING, logger="agent.session")
    path = tmp_path / "session.json"
    buf = ConversationBuffer(turn_limit=5)
    buf.append(Turn(role="user", content="\ud800"))  # lone high surrogate

    # Must not raise.
    save_session(buf, path)

    assert not path.exists()
    assert any("session save failed" in rec.message for rec in caplog.records)


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX-only, non-root — chmod doesn't restrict root or apply on Windows",
)
def test_save_session_logs_warning_on_permission_error_no_raise(tmp_path, caplog):
    """A non-writable parent directory logs WARNING and does NOT raise."""
    caplog.set_level(logging.WARNING, logger="agent.session")
    locked = tmp_path / "locked"
    locked.mkdir()
    path = locked / "session.json"
    locked.chmod(0o500)  # r-x only; no writes allowed
    try:
        # Must not raise.
        save_session(ConversationBuffer(turn_limit=5), path)
    finally:
        locked.chmod(0o700)  # restore so tmp_path can be cleaned up

    assert not path.exists()
    assert any("session save failed" in rec.message for rec in caplog.records)


# -----------------------------------------------------------------
# load_session — happy path, missing, corrupt, bad version, bad turn
# -----------------------------------------------------------------


def test_load_session_missing_file_returns_empty_buffer_no_warning(tmp_path, caplog):
    """First-run missing file is expected — empty buffer, no warning."""
    caplog.set_level(logging.WARNING, logger="agent.session")
    buf = load_session(tmp_path / "nope.json", turn_limit=5)
    assert len(buf) == 0
    assert buf.turn_limit == 5
    assert caplog.records == []


def test_load_session_corrupt_json_returns_empty_logs_warning(tmp_path, caplog):
    """A file that is not valid JSON produces an empty buffer + warning."""
    caplog.set_level(logging.WARNING, logger="agent.session")
    path = tmp_path / "session.json"
    path.write_text("{ not json", encoding="utf-8")

    buf = load_session(path, turn_limit=5)
    assert len(buf) == 0
    assert any("corrupt" in rec.message for rec in caplog.records)


def test_load_session_invalid_utf8_returns_empty_logs_warning(tmp_path, caplog):
    """#75 CR F2: a session.json that is not valid UTF-8 raises
    ``UnicodeDecodeError`` from ``read_text``. The load path must
    treat it as corrupt — WARNING + empty buffer, no escape."""
    caplog.set_level(logging.WARNING, logger="agent.session")
    path = tmp_path / "session.json"
    path.write_bytes(b"\xff\xfe\xff\x80")  # invalid UTF-8 byte sequence

    buf = load_session(path, turn_limit=5)
    assert len(buf) == 0
    assert any("session load read failed" in rec.message for rec in caplog.records)


def test_load_session_missing_version_field_returns_empty_logs_warning(tmp_path, caplog):
    """A payload without a 'version' field is treated as unsupported."""
    caplog.set_level(logging.WARNING, logger="agent.session")
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"turns": []}), encoding="utf-8")

    buf = load_session(path, turn_limit=5)
    assert len(buf) == 0
    assert any("unsupported schema version" in rec.message for rec in caplog.records)


def test_load_session_unknown_version_returns_empty_logs_warning(tmp_path, caplog):
    """A payload with a future / unknown version is discarded."""
    caplog.set_level(logging.WARNING, logger="agent.session")
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"version": 999, "turns": []}), encoding="utf-8")

    buf = load_session(path, turn_limit=5)
    assert len(buf) == 0
    assert any("unsupported schema version" in rec.message for rec in caplog.records)


def test_load_session_bool_version_treated_as_corrupt(tmp_path, caplog):
    """#75 CR F4: ``bool`` is a subclass of ``int`` in Python and
    ``True == 1`` is True, so a naive equality check would accept
    ``{"version": True}``. The ``type(v) is int`` guard must reject
    this."""
    caplog.set_level(logging.WARNING, logger="agent.session")
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"version": True, "turns": []}), encoding="utf-8")

    buf = load_session(path, turn_limit=5)
    assert len(buf) == 0
    assert any("unsupported schema version" in rec.message for rec in caplog.records)


def test_load_session_float_version_treated_as_corrupt(tmp_path, caplog):
    """#75 CR F4: ``1.0 == 1`` is True in Python, so an equality-only
    check would silently accept ``{"version": 1.0}``. The
    ``type(v) is int`` guard must reject non-int versions even when
    numerically equal to the schema constant."""
    caplog.set_level(logging.WARNING, logger="agent.session")
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"version": 1.0, "turns": []}), encoding="utf-8")

    buf = load_session(path, turn_limit=5)
    assert len(buf) == 0
    assert any("unsupported schema version" in rec.message for rec in caplog.records)


def test_load_session_bad_turn_naive_timestamp_discards_whole_file(tmp_path, caplog):
    """A naive (tz-unaware) timestamp fails Turn construction → whole
    file discarded (matches "discard if corrupt")."""
    caplog.set_level(logging.WARNING, logger="agent.session")
    path = tmp_path / "session.json"
    path.write_text(
        json.dumps(
            {
                "version": SCHEMA_VERSION,
                "turns": [
                    {"role": "user", "content": "ok", "timestamp": "2026-11-03T14:02:11"},
                ],
            }
        ),
        encoding="utf-8",
    )

    buf = load_session(path, turn_limit=5)
    assert len(buf) == 0
    assert any("bad turn" in rec.message for rec in caplog.records)


def test_load_session_bad_turn_invalid_role_discards_whole_file(tmp_path, caplog):
    """An invalid role on any turn → whole file discarded."""
    caplog.set_level(logging.WARNING, logger="agent.session")
    path = tmp_path / "session.json"
    path.write_text(
        json.dumps(
            {
                "version": SCHEMA_VERSION,
                "turns": [
                    {
                        "role": "user",
                        "content": "ok",
                        "timestamp": "2026-11-03T14:00:00+00:00",
                    },
                    {
                        "role": "system",  # invalid per Turn's _ROLES
                        "content": "bad",
                        "timestamp": "2026-11-03T14:00:05+00:00",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    buf = load_session(path, turn_limit=5)
    assert len(buf) == 0
    assert any("bad turn" in rec.message for rec in caplog.records)


def test_load_session_bad_turn_non_string_content_discards_whole_file(tmp_path, caplog):
    """#75 CR F5: Turn construction accepts ``content`` of any type
    (buffer.py doesn't validate it), so the loader must reject a
    non-string content field before calling Turn — matches the
    "wrong fields discard whole file" contract."""
    caplog.set_level(logging.WARNING, logger="agent.session")
    path = tmp_path / "session.json"
    path.write_text(
        json.dumps(
            {
                "version": SCHEMA_VERSION,
                "turns": [
                    {
                        "role": "user",
                        "content": 123,  # int, not str
                        "timestamp": "2026-11-04T12:00:00+00:00",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    buf = load_session(path, turn_limit=5)
    assert len(buf) == 0
    assert any("content is not a string" in rec.message for rec in caplog.records)


def test_load_session_bad_turn_non_dict_entry_discards_whole_file(tmp_path, caplog):
    """#75 CR F5: a non-object entry in ``turns`` (e.g. a bare string
    or a list) must be rejected before Turn construction."""
    caplog.set_level(logging.WARNING, logger="agent.session")
    path = tmp_path / "session.json"
    path.write_text(
        json.dumps({"version": SCHEMA_VERSION, "turns": ["not a turn object"]}),
        encoding="utf-8",
    )

    buf = load_session(path, turn_limit=5)
    assert len(buf) == 0
    assert any("turn is not an object" in rec.message for rec in caplog.records)


def test_load_session_bad_turn_missing_field_discards_whole_file(tmp_path, caplog):
    """A turn missing a required field → KeyError at Turn construction
    → whole file discarded.

    Probes the ``timestamp`` key specifically (not ``content``) so the
    #75 CR F5 content-shape guard doesn't short-circuit the test — F5
    rejects a non-string content at an earlier validation layer with
    a more specific message; the generic ``bad turn`` message is still
    what Turn's own KeyError path produces.
    """
    caplog.set_level(logging.WARNING, logger="agent.session")
    path = tmp_path / "session.json"
    path.write_text(
        json.dumps(
            {
                "version": SCHEMA_VERSION,
                "turns": [
                    {"role": "user", "content": "ok"},  # no timestamp
                ],
            }
        ),
        encoding="utf-8",
    )

    buf = load_session(path, turn_limit=5)
    assert len(buf) == 0
    assert any("bad turn" in rec.message for rec in caplog.records)


def test_load_session_turns_not_a_list_returns_empty_logs_warning(tmp_path, caplog):
    """'turns' must be a list — anything else is corrupt."""
    caplog.set_level(logging.WARNING, logger="agent.session")
    path = tmp_path / "session.json"
    path.write_text(
        json.dumps({"version": SCHEMA_VERSION, "turns": "not a list"}),
        encoding="utf-8",
    )

    buf = load_session(path, turn_limit=5)
    assert len(buf) == 0
    assert any("corrupt" in rec.message and "not a list" in rec.message for rec in caplog.records)


def test_load_session_top_level_not_dict_returns_empty_logs_warning(tmp_path, caplog):
    """Top-level JSON that isn't an object (e.g. a bare list) is corrupt."""
    caplog.set_level(logging.WARNING, logger="agent.session")
    path = tmp_path / "session.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    buf = load_session(path, turn_limit=5)
    assert len(buf) == 0
    assert any("not an object" in rec.message for rec in caplog.records)


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX-only, non-root — chmod doesn't restrict root or apply on Windows",
)
def test_load_session_permission_error_returns_empty_logs_warning(tmp_path, caplog):
    """Permission denied on read → WARNING + empty buffer."""
    caplog.set_level(logging.WARNING, logger="agent.session")
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"version": SCHEMA_VERSION, "turns": []}), encoding="utf-8")
    path.chmod(0o000)
    try:
        buf = load_session(path, turn_limit=5)
    finally:
        path.chmod(0o600)  # restore so tmp_path can be cleaned up

    assert len(buf) == 0
    assert any("session load read failed" in rec.message for rec in caplog.records)


def test_load_session_turn_limit_smaller_than_saved_fifo_truncates_oldest(tmp_path):
    """If the saved file holds more turns than the current run's
    turn_limit, deque maxlen keeps the most recent (FIFO eviction)."""
    path = tmp_path / "session.json"
    buf = ConversationBuffer(turn_limit=10)
    for i in range(10):
        role = "user" if i % 2 == 0 else "assistant"
        buf.append(Turn(role=role, content=f"turn-{i}"))
    save_session(buf, path)

    # Downsized run: turn_limit=3 keeps only the last three.
    loaded = load_session(path, turn_limit=3)
    assert len(loaded) == 3
    assert [t.content for t in loaded] == ["turn-7", "turn-8", "turn-9"]


# -----------------------------------------------------------------
# register_shutdown_save — idempotency + error swallowing
# -----------------------------------------------------------------


def test_register_shutdown_save_registers_exactly_one_atexit_callback(monkeypatch, tmp_path):
    """Two calls with the same (buffer, path) register exactly one
    atexit callback."""
    calls: list[tuple] = []
    monkeypatch.setattr(session.atexit, "register", lambda fn, *args: calls.append((fn, args)))

    buf = ConversationBuffer(turn_limit=5)
    path = tmp_path / "session.json"
    register_shutdown_save(buf, path)
    register_shutdown_save(buf, path)

    assert len(calls) == 1


def test_register_shutdown_save_registers_distinct_pairs_separately(monkeypatch, tmp_path):
    """Different buffers (or different paths) register separate callbacks."""
    calls: list[tuple] = []
    monkeypatch.setattr(session.atexit, "register", lambda fn, *args: calls.append((fn, args)))

    buf_a = ConversationBuffer(turn_limit=5)
    buf_b = ConversationBuffer(turn_limit=5)
    path = tmp_path / "session.json"
    register_shutdown_save(buf_a, path)
    register_shutdown_save(buf_b, path)

    assert len(calls) == 2


def test_register_shutdown_save_swallows_save_errors(monkeypatch, caplog, tmp_path):
    """The atexit callback must swallow save_session exceptions and log."""
    caplog.set_level(logging.WARNING, logger="agent.session")

    def boom(*a, **kw):
        raise RuntimeError("explode")

    monkeypatch.setattr(session, "save_session", boom)

    buf = ConversationBuffer(turn_limit=5)
    path = tmp_path / "session.json"
    _run_save(buf, path)  # must not raise
    assert any("atexit save failed" in rec.message for rec in caplog.records)


def test_atexit_persists_on_clean_exit(tmp_path):
    """Spawn a subprocess that registers a shutdown save, exit cleanly,
    and assert session.json is on disk with the expected content."""
    session_path = tmp_path / "session.json"
    script = f"""
import sys
sys.path.insert(0, {str(REPO_ROOT)!r})
from pathlib import Path
from agent.buffer import ConversationBuffer, Turn
from agent.session import register_shutdown_save
buf = ConversationBuffer(turn_limit=5)
buf.append(Turn(role='user', content='hi'))
buf.append(Turn(role='assistant', content='hello'))
register_shutdown_save(buf, Path({str(session_path)!r}))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert session_path.exists(), f"stderr={result.stderr}"
    data = json.loads(session_path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert [t["role"] for t in data["turns"]] == ["user", "assistant"]
    assert [t["content"] for t in data["turns"]] == ["hi", "hello"]
