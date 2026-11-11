"""Tests for :mod:`agent.memory_cli` (#80 — memory inspection CLI)."""

from __future__ import annotations

import json
import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import aiosqlite
import pytest

from agent.memory import add_memory, bootstrap_db, get_memory

# -----------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------


@pytest.fixture
async def bootstrapped_db(tmp_path: Path) -> Path:
    """A ready-to-use memory.db with v2 schema in place (no rows)."""
    path = tmp_path / "memory.db"
    await bootstrap_db(path)
    return path


@pytest.fixture
async def populated_db(bootstrapped_db: Path) -> Path:
    """``bootstrapped_db`` plus three memories across two kinds.

    Timestamps are seeded explicitly via raw INSERT so ordering is
    deterministic across both the DESC-ordered ``list`` path and the
    ASC-reversed ``export`` path.
    """
    base = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    seed = [
        ("fact", "FRIDAY uses SQLite", {}),
        ("preference", "likes Earl Grey", {"source": "test"}),
        ("person", "manager is Ana", {}),
    ]
    async with aiosqlite.connect(bootstrapped_db) as db:
        for i, (kind, content, meta) in enumerate(seed):
            ts = (base + timedelta(minutes=i)).isoformat()
            await db.execute(
                "INSERT INTO memories (created_at, updated_at, kind, content, metadata) "
                "VALUES (?, ?, ?, ?, ?)",
                (ts, ts, kind, content, json.dumps(meta)),
            )
        await db.commit()
    return bootstrapped_db


@pytest.fixture
def isolate_db(monkeypatch):
    """Factory: point ``default_memory_db_path`` at ``path``.

    Returns a function so each test parameterizes which DB to isolate
    against (``bootstrapped_db`` vs ``populated_db``).
    """

    def _apply(path: Path) -> None:
        monkeypatch.setattr(
            "agent.memory_cli.default_memory_db_path",
            lambda: path,
        )

    return _apply


async def _run_cli(argv: list[str]) -> int:
    """Dispatch into ``agent.loop.run`` under a given argv.

    Mirrors how ``python -m agent memory ...`` would invoke it. We
    import ``agent.loop`` here rather than at module scope so
    monkeypatches registered by each test apply in order. Awaited
    directly from the test's own event loop (pytest-asyncio auto-mode)
    because ``asyncio.run`` would collide with the already-running
    loop.
    """
    import agent.loop

    orig_argv = sys.argv[:]
    sys.argv = ["agent", *argv]
    try:
        return await agent.loop.run()
    finally:
        sys.argv = orig_argv


# =================================================================
# list subcommand
# =================================================================


async def test_list_empty_db_prints_no_memories_match_to_stderr_exit_0(
    bootstrapped_db, isolate_db, capsys
):
    isolate_db(bootstrapped_db)
    rc = await _run_cli(["memory", "list"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "No memories match." in captured.err
    assert captured.out == ""


async def test_list_text_format_renders_table_columns_with_truncation(
    bootstrapped_db, isolate_db, capsys
):
    """Row content > 60 chars triggers truncation with U+2026."""
    long = "a" * 70
    await add_memory(bootstrapped_db, kind="fact", content=long)
    isolate_db(bootstrapped_db)

    rc = await _run_cli(["memory", "list"])
    captured = capsys.readouterr()
    assert rc == 0
    out = captured.out
    # Header + truncated content ends with U+2026 (single codepoint).
    assert "id" in out and "created_at" in out and "kind" in out and "content" in out
    assert "a" * 60 + "…" in out
    # The full 70-char original must NOT appear untruncated.
    assert "a" * 70 not in out


async def test_list_json_format_round_trips_to_populated_memories(populated_db, isolate_db, capsys):
    isolate_db(populated_db)
    rc = await _run_cli(["memory", "list", "--format", "json"])
    captured = capsys.readouterr()
    assert rc == 0
    parsed = json.loads(captured.out)
    assert isinstance(parsed, list)
    assert len(parsed) == 3
    # Newest-first ordering (DESC created_at): seed order was
    # [fact, preference, person] with +0m / +1m / +2m, so DESC is
    # [person, preference, fact].
    assert [m["kind"] for m in parsed] == ["person", "preference", "fact"]
    for m in parsed:
        assert set(m.keys()) == {
            "id",
            "created_at",
            "updated_at",
            "kind",
            "content",
            "metadata",
        }


async def test_list_kind_filter_narrows_results(populated_db, isolate_db, capsys):
    isolate_db(populated_db)
    rc = await _run_cli(["memory", "list", "--kind", "preference", "--format", "json"])
    captured = capsys.readouterr()
    assert rc == 0
    parsed = json.loads(captured.out)
    assert len(parsed) == 1
    assert parsed[0]["kind"] == "preference"


async def test_list_limit_caps_results(populated_db, isolate_db, capsys):
    isolate_db(populated_db)
    rc = await _run_cli(["memory", "list", "--limit", "2", "--format", "json"])
    captured = capsys.readouterr()
    assert rc == 0
    parsed = json.loads(captured.out)
    assert len(parsed) == 2


# =================================================================
# search subcommand
# =================================================================


async def test_search_happy_path_returns_matching_rows(populated_db, isolate_db, capsys):
    isolate_db(populated_db)
    rc = await _run_cli(["memory", "search", "SQLite", "--format", "json"])
    captured = capsys.readouterr()
    assert rc == 0
    parsed = json.loads(captured.out)
    assert len(parsed) == 1
    assert "SQLite" in parsed[0]["content"]


async def test_search_no_matches_prints_no_memories_match_exit_0(populated_db, isolate_db, capsys):
    isolate_db(populated_db)
    rc = await _run_cli(["memory", "search", "zzznonexistentterm"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "No memories match." in captured.err


async def test_search_empty_query_errors_exit_2(bootstrapped_db, isolate_db, capsys):
    isolate_db(bootstrapped_db)
    rc = await _run_cli(["memory", "search", "   "])
    captured = capsys.readouterr()
    assert rc == 2
    assert "query must be non-empty" in captured.err


async def test_search_limit_overrides_default_k(bootstrapped_db, isolate_db, capsys):
    """Default k is 5; ``--limit 3`` caps below that."""
    for i in range(10):
        await add_memory(bootstrapped_db, kind="fact", content=f"coffee match {i}")
    isolate_db(bootstrapped_db)

    rc = await _run_cli(["memory", "search", "coffee", "--limit", "3", "--format", "json"])
    captured = capsys.readouterr()
    assert rc == 0
    parsed = json.loads(captured.out)
    assert len(parsed) == 3


# =================================================================
# forget subcommand
# =================================================================


async def test_forget_missing_id_prints_not_found_exit_1(bootstrapped_db, isolate_db, capsys):
    isolate_db(bootstrapped_db)
    rc = await _run_cli(["memory", "forget", "99999"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "No memory with id=99999" in captured.err


async def test_forget_tty_decline_prints_aborted_exit_1_row_survives(
    bootstrapped_db, isolate_db, monkeypatch, capsys
):
    new_id = await add_memory(bootstrapped_db, kind="fact", content="disposable")
    isolate_db(bootstrapped_db)
    monkeypatch.setattr("agent.memory_cli._stdin_is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_: "n")

    rc = await _run_cli(["memory", "forget", str(new_id)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "aborted." in captured.err
    # Row survives the decline.
    assert await get_memory(bootstrapped_db, new_id) is not None


async def test_forget_tty_accept_prints_forgot_exit_0_row_gone(
    bootstrapped_db, isolate_db, monkeypatch, capsys
):
    new_id = await add_memory(bootstrapped_db, kind="fact", content="disposable")
    isolate_db(bootstrapped_db)
    monkeypatch.setattr("agent.memory_cli._stdin_is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_: "y")

    rc = await _run_cli(["memory", "forget", str(new_id)])
    captured = capsys.readouterr()
    assert rc == 0
    assert f"Forgot memory {new_id}." in captured.out
    assert await get_memory(bootstrapped_db, new_id) is None


async def test_forget_yes_flag_bypasses_prompt(bootstrapped_db, isolate_db, monkeypatch, capsys):
    new_id = await add_memory(bootstrapped_db, kind="fact", content="disposable")
    isolate_db(bootstrapped_db)
    monkeypatch.setattr("agent.memory_cli._stdin_is_tty", lambda: True)
    input_mock = MagicMock(side_effect=AssertionError("input() should not be called"))
    monkeypatch.setattr("builtins.input", input_mock)

    rc = await _run_cli(["memory", "forget", str(new_id), "--yes"])
    assert rc == 0
    assert input_mock.call_count == 0
    assert await get_memory(bootstrapped_db, new_id) is None


async def test_forget_non_tty_stdin_bypasses_prompt(
    bootstrapped_db, isolate_db, monkeypatch, capsys
):
    new_id = await add_memory(bootstrapped_db, kind="fact", content="disposable")
    isolate_db(bootstrapped_db)
    monkeypatch.setattr("agent.memory_cli._stdin_is_tty", lambda: False)
    input_mock = MagicMock(side_effect=AssertionError("input() should not be called"))
    monkeypatch.setattr("builtins.input", input_mock)

    rc = await _run_cli(["memory", "forget", str(new_id)])
    assert rc == 0
    assert input_mock.call_count == 0
    assert await get_memory(bootstrapped_db, new_id) is None


async def test_forget_content_with_newline_preview_truncates_safely(
    bootstrapped_db, isolate_db, monkeypatch, capsys
):
    """Content containing embedded newlines + >80 chars must produce a
    one-line prompt (newlines replaced with spaces) ending in U+2026."""
    content = "first line\nsecond line " + "x" * 80
    new_id = await add_memory(bootstrapped_db, kind="fact", content=content)
    isolate_db(bootstrapped_db)
    monkeypatch.setattr("agent.memory_cli._stdin_is_tty", lambda: True)

    captured_prompts: list[str] = []

    def record_prompt(prompt: str) -> str:
        captured_prompts.append(prompt)
        return "n"

    monkeypatch.setattr("builtins.input", record_prompt)

    rc = await _run_cli(["memory", "forget", str(new_id)])
    assert rc == 1  # declined
    assert len(captured_prompts) == 1
    prompt = captured_prompts[0]
    # Prompt is a single line (no literal \n inside the preview).
    assert "\n" not in prompt
    # Truncation marker present.
    assert "…" in prompt


# =================================================================
# export subcommand
# =================================================================


async def test_export_writes_jsonl_file_with_header_and_data_lines(
    populated_db, isolate_db, tmp_path, capsys
):
    isolate_db(populated_db)
    target = tmp_path / "out.jsonl"
    rc = await _run_cli(["memory", "export", str(target)])
    captured = capsys.readouterr()
    assert rc == 0
    assert target.exists()
    assert f"Exported 3 memories to {target}." in captured.out

    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4  # header + 3 data rows
    header = json.loads(lines[0])
    assert header["schema_version"] == 2
    assert header["count"] == 3
    # exported_at parses as tz-aware ISO 8601.
    parsed_ts = datetime.fromisoformat(header["exported_at"])
    assert parsed_ts.tzinfo is not None

    data = [json.loads(line) for line in lines[1:]]
    # ASC created_at ordering on disk — seed order was
    # [fact, preference, person], which IS chronological.
    assert [row["kind"] for row in data] == ["fact", "preference", "person"]
    for row in data:
        assert set(row.keys()) == {
            "id",
            "created_at",
            "updated_at",
            "kind",
            "content",
            "metadata",
        }


async def test_export_empty_db_writes_header_only_with_count_0(
    bootstrapped_db, isolate_db, tmp_path, capsys
):
    isolate_db(bootstrapped_db)
    target = tmp_path / "out.jsonl"
    rc = await _run_cli(["memory", "export", str(target)])
    assert rc == 0
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    header = json.loads(lines[0])
    assert header["count"] == 0
    assert header["schema_version"] == 2


async def test_export_refuses_existing_target_without_force_exit_1(
    populated_db, isolate_db, tmp_path, capsys
):
    isolate_db(populated_db)
    target = tmp_path / "out.jsonl"
    target.write_text("OLD CONTENT", encoding="utf-8")
    rc = await _run_cli(["memory", "export", str(target)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "exists" in captured.err
    assert target.read_text(encoding="utf-8") == "OLD CONTENT"


async def test_export_force_overwrites_target_atomically(
    populated_db, isolate_db, tmp_path, capsys
):
    isolate_db(populated_db)
    target = tmp_path / "out.jsonl"
    target.write_text("OLD CONTENT", encoding="utf-8")
    rc = await _run_cli(["memory", "export", str(target), "--force"])
    assert rc == 0
    new_contents = target.read_text(encoding="utf-8")
    assert "OLD CONTENT" not in new_contents
    header = json.loads(new_contents.splitlines()[0])
    assert header["count"] == 3


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX-only, non-root (mode bits are a no-op on Windows; root ignores them)",
)
async def test_export_sets_0o600_mode_on_posix(populated_db, isolate_db, tmp_path, capsys):
    isolate_db(populated_db)
    target = tmp_path / "out.jsonl"
    rc = await _run_cli(["memory", "export", str(target)])
    assert rc == 0
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


async def test_export_missing_parent_dir_exit_1(populated_db, isolate_db, tmp_path, capsys):
    """Parent dir that doesn't exist → operational failure, exit 1."""
    isolate_db(populated_db)
    missing = tmp_path / "nope" / "out.jsonl"
    rc = await _run_cli(["memory", "export", str(missing)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "does not exist" in captured.err


async def test_export_refuses_over_ceiling(populated_db, isolate_db, tmp_path, monkeypatch, capsys):
    """When ``list_memories`` returns more rows than the ceiling, the
    export refuses with a clear message and exit 1."""
    from agent.memory import Memory

    isolate_db(populated_db)
    monkeypatch.setattr("agent.memory_cli._EXPORT_MAX_ROWS", 2)

    async def fake_list_memories(db_path, kind=None, limit=100):
        base = datetime(2026, 1, 1, tzinfo=UTC)
        return [
            Memory(
                id=i,
                created_at=base,
                updated_at=base,
                kind="fact",
                content=f"row {i}",
                metadata={},
            )
            for i in range(1, limit + 1)
        ]

    monkeypatch.setattr("agent.memory_cli.list_memories", fake_list_memories)

    target = tmp_path / "out.jsonl"
    rc = await _run_cli(["memory", "export", str(target)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "ceiling" in captured.err
    assert "streaming export" in captured.err
    assert not target.exists()


# =================================================================
# Dispatch wiring
# =================================================================


async def test_memory_list_dispatch_skips_warm_encoder_and_session_state(
    bootstrapped_db, isolate_db, monkeypatch
):
    """Memory CLI dispatch must NOT call warm_encoder,
    _init_session_state, or register_shutdown_save — those belong to
    the voice/text branches."""
    isolate_db(bootstrapped_db)

    warm_mock = MagicMock()

    async def _warm(timeout: float = 15.0) -> None:
        warm_mock()

    init_session_mock = MagicMock()
    register_shutdown_mock = MagicMock()

    monkeypatch.setattr("agent.loop.warm_encoder", _warm)
    monkeypatch.setattr("agent.loop._init_session_state", init_session_mock)
    monkeypatch.setattr("agent.loop.register_shutdown_save", register_shutdown_mock)

    rc = await _run_cli(["memory", "list"])
    assert rc == 0
    assert warm_mock.call_count == 0
    assert init_session_mock.call_count == 0
    assert register_shutdown_mock.call_count == 0


# =================================================================
# Coverage-fill: argparse validation, table format, defensive branches
# =================================================================


async def test_positive_int_rejects_non_integer_input(bootstrapped_db, isolate_db, capsys):
    """``_positive_int`` argparse type rejects ``--limit abc`` with
    argparse-misuse semantics (exit 2)."""
    isolate_db(bootstrapped_db)
    with pytest.raises(SystemExit) as exc_info:
        await _run_cli(["memory", "list", "--limit", "abc"])
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "must be an int" in captured.err


async def test_positive_int_rejects_zero(bootstrapped_db, isolate_db, capsys):
    """``_positive_int`` argparse type rejects ``--limit 0`` with
    argparse-misuse semantics (exit 2)."""
    isolate_db(bootstrapped_db)
    with pytest.raises(SystemExit) as exc_info:
        await _run_cli(["memory", "list", "--limit", "0"])
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "must be a positive int" in captured.err


async def test_run_memory_cli_unknown_subcommand_exit_2(bootstrapped_db, isolate_db, capsys):
    """Directly exercise ``run_memory_cli`` with a fabricated unknown
    subcommand (unreachable via argparse's ``required=True`` on the
    subparsers group, but the defensive branch still exists)."""
    import argparse as _argparse

    from agent.memory_cli import run_memory_cli

    isolate_db(bootstrapped_db)
    args = _argparse.Namespace(memory_subcommand="bogus")
    rc = await run_memory_cli(args)
    captured = capsys.readouterr()
    assert rc == 2
    assert "unknown memory subcommand" in captured.err


async def test_list_empty_db_json_format_prints_bracket_pair_on_stdout(
    bootstrapped_db, isolate_db, capsys
):
    """``memory list --format json`` on an empty DB prints ``[]`` on
    stdout (NOT the human-readable stderr message) so a JSON consumer
    parses cleanly."""
    isolate_db(bootstrapped_db)
    rc = await _run_cli(["memory", "list", "--format", "json"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "[]"
    # stderr must NOT carry the text "No memories match." in JSON mode.
    assert "No memories match." not in captured.err


async def test_search_empty_result_json_format_prints_bracket_pair_on_stdout(
    bootstrapped_db, isolate_db, capsys
):
    """``memory search --format json`` on no-hits prints ``[]`` on
    stdout."""
    isolate_db(bootstrapped_db)
    rc = await _run_cli(["memory", "search", "zzzmissing", "--format", "json"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "[]"


async def test_search_text_format_with_matches(populated_db, isolate_db, capsys):
    """``memory search --format text`` (the default) on a matching
    query renders the table rather than JSON."""
    isolate_db(populated_db)
    rc = await _run_cli(["memory", "search", "SQLite"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "SQLite" in captured.out
    # Table header present.
    assert "id" in captured.out and "created_at" in captured.out


async def test_list_text_format_with_short_content_no_truncation(populated_db, isolate_db, capsys):
    """Rows whose content fits inside ``_CONTENT_PREVIEW_CHARS`` are
    rendered untruncated (no U+2026)."""
    isolate_db(populated_db)
    rc = await _run_cli(["memory", "list"])
    captured = capsys.readouterr()
    assert rc == 0
    # The populated fixture's rows are all short (< 60 chars).
    assert "…" not in captured.out
    # All three rows' content appears intact.
    assert "FRIDAY uses SQLite" in captured.out
    assert "likes Earl Grey" in captured.out
    assert "manager is Ana" in captured.out


async def test_forget_delete_returns_false_race_prints_not_found_exit_1(
    bootstrapped_db, isolate_db, monkeypatch, capsys
):
    """Defensive branch: ``delete_memory`` returning ``False`` after a
    successful ``get_memory`` surfaces as the not-found error + exit 1
    rather than a false "Forgot" message."""
    new_id = await add_memory(bootstrapped_db, kind="fact", content="disposable")
    isolate_db(bootstrapped_db)
    monkeypatch.setattr("agent.memory_cli._stdin_is_tty", lambda: False)

    async def fake_delete(db_path, memory_id):
        return False

    monkeypatch.setattr("agent.memory_cli.delete_memory", fake_delete)

    rc = await _run_cli(["memory", "forget", str(new_id)])
    captured = capsys.readouterr()
    assert rc == 1
    assert f"No memory with id={new_id}" in captured.err


async def test_export_writer_oserror_surfaces_as_export_failed_exit_1(
    populated_db, isolate_db, tmp_path, monkeypatch, capsys
):
    """A filesystem error inside the writer path surfaces as
    ``"export failed: ..."`` on stderr + exit 1 (operational failure).
    Also exercises the tempfile-cleanup finally branch — mkstemp
    succeeds, then os.replace raises, so the ``.tmp`` file must be
    unlinked to avoid leaking residue in the user's data dir."""
    isolate_db(populated_db)
    target = tmp_path / "out.jsonl"

    real_replace = os.replace
    boom = OSError("simulated disk failure")

    def fake_replace(src, dst):
        # Leave the tempfile in place so the finally branch's unlink
        # path actually has work to do.
        raise boom

    monkeypatch.setattr("agent.memory_cli.os.replace", fake_replace)

    rc = await _run_cli(["memory", "export", str(target)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "export failed" in captured.err
    assert "simulated disk failure" in captured.err
    assert not target.exists()
    # Tempfile must have been cleaned up — no .tmp residue next to target.
    residues = list(target.parent.glob(".memory_export.*.tmp"))
    assert residues == [], f"tempfile leaked: {residues!r}"

    # Sanity: with a real os.replace, the same export now succeeds.
    monkeypatch.setattr("agent.memory_cli.os.replace", real_replace)
    rc2 = await _run_cli(["memory", "export", str(target)])
    assert rc2 == 0


def test_stdin_is_tty_delegates_to_real_sys_stdin():
    """Bare call to ``_stdin_is_tty`` returns a bool from the real
    ``sys.stdin.isatty()``. Covers the module-indirect wrapper line."""
    from agent.memory_cli import _stdin_is_tty

    result = _stdin_is_tty()
    assert isinstance(result, bool)


async def test_memory_list_dispatch_calls_bootstrap_db(bootstrapped_db, isolate_db, monkeypatch):
    """Memory CLI dispatch MUST bootstrap_db so a brand-new install's
    first ``memory list`` prints the empty-result message rather than
    hitting ``FileNotFoundError``."""
    isolate_db(bootstrapped_db)

    bootstrap_mock = MagicMock()

    async def _bootstrap(path: Path) -> None:
        bootstrap_mock(path)

    monkeypatch.setattr("agent.memory_cli.bootstrap_db", _bootstrap)
    rc = await _run_cli(["memory", "list"])
    assert rc == 0
    assert bootstrap_mock.call_count == 1
    assert bootstrap_mock.call_args.args[0] == bootstrapped_db
