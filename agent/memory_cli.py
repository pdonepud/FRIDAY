"""Memory inspection CLI (#80) — ``python -m agent memory ...``.

Four subcommands under a ``memory`` positional on the existing
``python -m agent`` entry point, backed by the long-term DB from #76
and the ``list_memories`` surface this PR adds to :mod:`agent.memory`.

## Scope

**Does:** list, FTS5-search, forget (delete), and export memory rows
for the solo-user ``memory.db`` under ``agent.paths.get_data_dir()``.
Owns the argparse subparser factory, the four subcommand handlers,
the human-readable table + JSON output formatters, the TTY-detect
confirmation prompt for ``forget``, and the atomic tempfile JSONL
export writer. The :mod:`agent.memory` module stays the sole SQL
seam per ADR-0002 — this file issues zero raw SQL and reaches the
DB only through ``list_memories`` / ``search_memories`` /
``get_memory`` / ``delete_memory``.

**Does not:** call Claude (no :mod:`agent.claude` import), touch the
conversation buffer (no :mod:`agent.session` / :mod:`agent.buffer`
import), open an audio stream, or start the token-counter warmup. On
dispatch from :func:`agent.loop.run`, ``warm_encoder`` and
``_init_session_state`` are explicitly skipped (see the module-level
dispatch edit in ``agent/loop.py``). Full ADR-0006 Open-Question-#3
confirmation-gate semantics stay deferred to Tier 7.

## Exit codes

Taxonomy shared across all four subcommands:

* **0** — success. Includes empty result sets from ``list`` / ``search``
  (an empty DB is not an error).
* **1** — operational failure: missing memory id on ``forget``,
  user declined at the ``forget`` confirmation prompt, I/O failure on
  ``export``, export target exists without ``--force``, the
  :data:`_EXPORT_MAX_ROWS` ceiling hit on ``export``.
* **2** — argparse / parser misuse: empty-after-strip query on
  ``search``, a non-positive ``--limit`` (rejected at argparse parse
  time via :func:`_positive_int`), an unknown subcommand.

## Export format

JSONL. Line 1 is a header carrying ``schema_version`` (mirrors
:data:`agent.memory.SCHEMA_VERSION`), ``exported_at`` (ISO 8601 UTC
tz-aware), and ``count`` (exact count of data lines that follow).
Lines 2..N+1 are one ``Memory`` per line, serialized via the private
:func:`_memory_to_dict` helper that ``--format json`` on ``list`` /
``search`` also uses, so the on-wire shape never diverges between
stdout and file surfaces. Rows are ordered ``created_at`` ASC
(chronological) so a hypothetical replay-import walks the user's
history forward in time. File is written atomically via
``tempfile.mkstemp`` + ``os.replace`` + POSIX ``0o600`` — mirrors
:mod:`agent.session`'s precedent (ADR-0006 §Short-term).
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import sys
import tempfile
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from agent.memory import (
    SCHEMA_VERSION,
    Memory,
    bootstrap_db,
    default_memory_db_path,
    delete_memory,
    get_memory,
    list_memories,
    search_memories,
)
from agent.session import default_session_path

#: Default cap on ``list`` output rows when ``--limit`` is not passed.
#: Matches ADR-0006 §Public surface's ``list_memories(limit=100)``
#: default so the CLI surface and the DB surface agree.
_DEFAULT_LIST_LIMIT: Final[int] = 100

#: Default ``k`` for the ``search`` subcommand when ``--limit`` is not
#: passed. Matches :data:`agent.memory._FTS_TOPK_DEFAULT` (5) per
#: ADR-0006 Implementation Notes.
_DEFAULT_SEARCH_LIMIT: Final[int] = 5

#: Hard ceiling for ``export`` rows. TODO(#80-tuning): add a streaming
#: export variant if a real user ever exceeds this on a single-user
#: ``memory.db``. Current ceiling is defense-in-depth against
#: pathological growth — the single-pass in-memory export reads the
#: full result list through :func:`agent.memory.list_memories` before
#: writing, so a cap bounds worst-case footprint without needing a
#: schema change.
_EXPORT_MAX_ROWS: Final[int] = 1_000_000

#: Truncation ceiling for the ``content`` column in the text
#: ``list`` / ``search`` table.
_CONTENT_PREVIEW_CHARS: Final[int] = 60

#: Minimum column width for ``kind`` in the text table so the header
#: has a stable minimum shape regardless of result-set kinds.
_KIND_MIN_WIDTH: Final[int] = 10

#: Truncation ceiling for the ``forget`` confirmation-prompt preview.
_FORGET_PREVIEW_CHARS: Final[int] = 80

#: Single-codepoint ellipsis (U+2026) used by both table truncation
#: and forget-prompt truncation. ASCII ``"..."`` would misalign the
#: fixed-width table by two characters.
_HORIZONTAL_ELLIPSIS: Final[str] = "…"

#: Replacement codepoint substituted for terminal-unsafe characters by
#: :func:`_sanitize_for_terminal`. ASCII ``?`` keeps the fixed-width
#: table aligned (single column) and is visible on any encoding.
_SANITIZE_REPLACEMENT: Final[str] = "?"


def _protected_paths() -> set[Path]:
    """FRIDAY's own data-file paths (resolved) that ``export`` must not overwrite.

    CR F1 (PR #107): ``memory export <memory.db> --force`` would
    otherwise clobber the SQLite file with JSONL and brick the next
    voice/text session. ``--force`` is meant for overwriting arbitrary
    user-chosen files, not for overriding data integrity on FRIDAY's
    own stores. :func:`_cmd_export` consults this set before any
    filesystem I/O.

    Resolving guarantees uniform treatment of symlinks, ``.`` / ``..``
    segments, and relative paths — a user running
    ``memory export ./memory.db`` from inside the data directory still
    hits the guard.
    """
    return {
        default_memory_db_path().resolve(),
        default_session_path().resolve(),
    }


def _sanitize_for_terminal(text: str) -> str:
    """Replace terminal-unsafe characters with ``?`` for text-mode rendering.

    CR F2 (PR #107): ``_format_memories_table`` and ``_confirm_forget``
    print ``memory.kind`` / ``memory.content`` directly to stdout.
    :func:`agent.memory.add_memory` validates both as non-empty strings
    but does NOT filter control characters, so a seeded memory whose
    content contains an ANSI escape sequence (e.g. ``\\x1b[2J`` clears
    the screen; ``\\x1b]0;...\\x07`` sets the terminal title) would
    execute when printed in text mode.

    Covers:

    * Unicode category ``Cc`` (C0 + C1 control chars: ESC ``\\x1b``,
      newline ``\\x0a``, carriage return ``\\x0d``, TAB ``\\x09``,
      DEL ``\\x7f``, …). Also subsumes the ad-hoc newline replacement
      the pre-CR :func:`_truncate_preview` performed.
    * Line separator U+2028 and paragraph separator U+2029 (category
      ``Zl`` / ``Zp``; not ``Cc`` but visually line-breaking in some
      terminals).

    Applied only in the human-readable text renderers. JSON
    (``--format json`` on ``list`` / ``search``) and JSONL export go
    through ``json.dumps``, which escapes control characters natively
    and preserves on-wire fidelity for ``jq`` consumers and
    backup/restore use cases.
    """
    return "".join(
        _SANITIZE_REPLACEMENT if (unicodedata.category(ch) == "Cc" or ch in "  ") else ch
        for ch in text
    )


def _positive_int(raw: str) -> int:
    """argparse ``type`` function: reject non-positive ``--limit``.

    ``argparse.ArgumentTypeError`` propagates as argparse's own
    exit-code-2 misuse path, keeping the exit-code taxonomy consistent
    with other argparse misuse (unknown subcommand, missing required
    positional, etc.) without adding a per-handler validation branch.
    """
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be an int; got {raw!r}") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive int; got {value}")
    return value


def build_memory_subparser(parent: argparse._SubParsersAction) -> None:
    """Register the ``memory`` subparser (with four sub-subparsers) on ``parent``.

    Called once from :func:`agent.loop.run` while the top-level parser
    is being built. The outer ``parent`` subparsers group is created
    with ``required=False`` so ``python -m agent`` alone still
    dispatches to the voice loop; this function adds ``memory`` as one
    valid subcommand without changing that fallthrough.
    """
    memory_p = parent.add_parser(
        "memory",
        help="Inspect long-term memory (list / search / forget / export).",
        description="FRIDAY memory inspection CLI (#80).",
    )
    subs = memory_p.add_subparsers(
        dest="memory_subcommand",
        required=True,
        metavar="SUBCOMMAND",
    )

    list_p = subs.add_parser("list", help="List memories (newest first).")
    list_p.add_argument(
        "--kind",
        default=None,
        help="Filter to rows whose kind column is exactly this value.",
    )
    list_p.add_argument(
        "--limit",
        type=_positive_int,
        default=_DEFAULT_LIST_LIMIT,
        help=f"Cap on returned rows (default {_DEFAULT_LIST_LIMIT}).",
    )
    list_p.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: fixed-width text table).",
    )

    search_p = subs.add_parser("search", help="FTS5 keyword search over content.")
    search_p.add_argument("query", help="Search terms (bag-of-words, implicit-AND).")
    search_p.add_argument(
        "--limit",
        type=_positive_int,
        default=_DEFAULT_SEARCH_LIMIT,
        help=f"Cap on returned rows (default {_DEFAULT_SEARCH_LIMIT}).",
    )
    search_p.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: fixed-width text table).",
    )

    forget_p = subs.add_parser("forget", help="Delete a memory by id.")
    forget_p.add_argument("id", type=int, help="Memory row id.")
    forget_p.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip the interactive confirmation prompt.",
    )

    export_p = subs.add_parser("export", help="Export all memories as JSONL.")
    export_p.add_argument("path", type=Path, help="Destination JSONL file.")
    export_p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the destination file if it already exists.",
    )


async def run_memory_cli(args: argparse.Namespace) -> int:
    """Dispatch the parsed ``memory`` subcommand to its handler.

    Called by :func:`agent.loop.run` when ``args.subcommand == "memory"``.
    First idempotently bootstraps ``memory.db`` so a brand-new install
    never hits a ``FileNotFoundError`` on ``memory list``. Then routes
    to one of ``_cmd_list`` / ``_cmd_search`` / ``_cmd_forget`` /
    ``_cmd_export`` and returns its int exit code.

    An unknown ``memory_subcommand`` (should be unreachable since the
    subparser is declared ``required=True``) exits 2 to keep the
    argparse-misuse exit code consistent.
    """
    await bootstrap_db(default_memory_db_path())
    sub = args.memory_subcommand
    if sub == "list":
        return await _cmd_list(args)
    if sub == "search":
        return await _cmd_search(args)
    if sub == "forget":
        return await _cmd_forget(args)
    if sub == "export":
        return await _cmd_export(args)
    print(f"error: unknown memory subcommand {sub!r}", file=sys.stderr)
    return 2


async def _cmd_list(args: argparse.Namespace) -> int:
    """Handle ``memory list``.

    Empty-result UX: text mode prints ``"No memories match."`` to
    stderr and exits 0 (empty is not an error); json mode prints
    ``[]`` to stdout so a JSON consumer can parse without a special
    case.
    """
    memories = await list_memories(
        default_memory_db_path(),
        kind=args.kind,
        limit=args.limit,
    )
    if not memories:
        if args.format == "json":
            print("[]")
        else:
            print("No memories match.", file=sys.stderr)
        return 0
    if args.format == "json":
        print(_format_memories_json(memories))
    else:
        print(_format_memories_table(memories))
    return 0


async def _cmd_search(args: argparse.Namespace) -> int:
    """Handle ``memory search "query"``.

    Empty-after-strip queries exit 2 (argparse-misuse semantics —
    passing only whitespace is a parser-level user error, not an
    operational one). Empty result sets exit 0 like ``list``.
    """
    query = args.query.strip()
    if not query:
        print("error: query must be non-empty.", file=sys.stderr)
        return 2
    memories = await search_memories(
        default_memory_db_path(),
        query,
        k=args.limit,
    )
    if not memories:
        if args.format == "json":
            print("[]")
        else:
            print("No memories match.", file=sys.stderr)
        return 0
    if args.format == "json":
        print(_format_memories_json(memories))
    else:
        print(_format_memories_table(memories))
    return 0


async def _cmd_forget(args: argparse.Namespace) -> int:
    """Handle ``memory forget <id>``.

    Unix ``rm -i`` posture: an interactive TTY stdin prompts for
    ``[y/N]``; piped stdin or ``--yes`` skips the prompt. User
    decline exits 1 (operational failure; the request was refused),
    missing id exits 1 (also operational), successful delete exits 0.
    """
    db_path = default_memory_db_path()
    memory = await get_memory(db_path, args.id)
    if memory is None:
        print(f"No memory with id={args.id}", file=sys.stderr)
        return 1
    if not _confirm_forget(memory, assume_yes=args.yes):
        print("aborted.", file=sys.stderr)
        return 1
    removed = await delete_memory(db_path, args.id)
    if not removed:
        # Defensive: the row vanished between the get and the delete.
        # Can't happen under the single-process model, but surface it
        # cleanly rather than lying with "Forgot" when nothing was.
        print(f"No memory with id={args.id}", file=sys.stderr)
        return 1
    print(f"Forgot memory {args.id}.")
    return 0


async def _cmd_export(args: argparse.Namespace) -> int:
    """Handle ``memory export <path.jsonl>``.

    Flow:

    1. Verify the target directory exists (argparse ``type=Path``
       guarantees the arg parses, but not that the parent dir is real).
    2. Fetch up to ``_EXPORT_MAX_ROWS + 1`` memories via
       :func:`agent.memory.list_memories`; refuse if the DB has more
       than the ceiling (indicated by a result length > the ceiling).
    3. Reverse the DESC result into chronological ASC order.
    4. Write via :func:`_write_jsonl_export` — atomic tempfile +
       ``os.replace`` + POSIX ``0o600`` (mirrors
       :func:`agent.session.save_session`). The writer raises
       :class:`FileExistsError` on target-exists-without-force,
       :class:`OSError` on any I/O failure, and :class:`UnicodeError`
       on any content that can't serialize to UTF-8 (which
       :func:`agent.memory.add_memory` already guards against at the
       write seam, so this is defense-in-depth).
    """
    path: Path = args.path
    # CR F1: refuse to overwrite FRIDAY's own data files even under
    # ``--force``. ``--force`` is for arbitrary user-owned targets; the
    # memory DB and the session JSON are our own integrity contract.
    # Check FIRST — before parent-is-dir / exists probes — so a bad
    # target never touches the filesystem, and ``resolve()`` handles
    # ``.`` / ``..`` / symlinks uniformly.
    resolved_target = path.resolve()
    if resolved_target in _protected_paths():
        print(
            f"error: refusing to export over FRIDAY's own data file "
            f"({resolved_target}). Pick a different target.",
            file=sys.stderr,
        )
        return 1
    if not path.parent.is_dir():
        print(
            f"export target directory {path.parent} does not exist",
            file=sys.stderr,
        )
        return 1
    # CR R2 F3: pre-check lives in _cmd_export (not inside the writer)
    # so it stays a fast user-friendly error distinct from the writer's
    # no-clobber install-step race detection. A FileExistsError from
    # the writer is now ALWAYS the TOCTOU race case and flows through
    # the "export failed: ..." OSError handler below with the writer's
    # own "created mid-write; re-run with --force" message intact.
    if path.exists() and not args.force:
        print(
            f"target {path} exists (pass --force to overwrite)",
            file=sys.stderr,
        )
        return 1
    db_path = default_memory_db_path()
    # Query one past the ceiling so a result-length check distinguishes
    # "at or below the ceiling" (export proceeds) from "over the
    # ceiling" (refuse) without a separate COUNT(*) query.
    memories_desc = await list_memories(db_path, limit=_EXPORT_MAX_ROWS + 1)
    if len(memories_desc) > _EXPORT_MAX_ROWS:
        print(
            f"error: refusing to export {_EXPORT_MAX_ROWS:,}+ rows "
            f"(ceiling: {_EXPORT_MAX_ROWS:,}). Export a subset with --kind, "
            f"or file a request for streaming export.",
            file=sys.stderr,
        )
        return 1
    memories = list(reversed(memories_desc))
    try:
        await _write_jsonl_export(memories, path, force=args.force)
    except (OSError, UnicodeError) as exc:
        # OSError here includes the writer's own FileExistsError on
        # the no-clobber TOCTOU-race case (FileExistsError IS an
        # OSError) with the "created mid-write" message, plus the
        # hard-link-unsupported OSError on FAT/exFAT per CR R2 F3.
        print(f"export failed: {exc}", file=sys.stderr)
        return 1
    print(f"Exported {len(memories)} memories to {path}.")
    return 0


# -----------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------


def _memory_to_dict(memory: Memory) -> dict[str, object]:
    """Shared Memory → dict serializer for JSON stdout + JSONL export.

    Timestamps serialize via :meth:`datetime.datetime.isoformat`
    (tz-aware round-trip preserved); ``metadata`` passes through as
    the dict :class:`agent.memory.Memory` already validated it to be.
    """
    return {
        "id": memory.id,
        "created_at": memory.created_at.isoformat(),
        "updated_at": memory.updated_at.isoformat(),
        "kind": memory.kind,
        "content": memory.content,
        "metadata": memory.metadata,
    }


def _format_memories_json(memories: list[Memory]) -> str:
    """JSON array of :func:`_memory_to_dict` entries, pretty-indented."""
    return json.dumps(
        [_memory_to_dict(m) for m in memories],
        ensure_ascii=False,
        indent=2,
    )


def _format_memories_table(memories: list[Memory]) -> str:
    """Fixed-width ``id | created_at | kind | content`` table.

    Column widths:

    * ``id`` — right-aligned, min width 4.
    * ``created_at`` — UTC, ``YYYY-MM-DD HH:MM:SS`` (19 chars).
    * ``kind`` — left-aligned, width = ``max(_KIND_MIN_WIDTH,
      max(len(kind) for kind in result))``.
    * ``content`` — truncated to ``_CONTENT_PREVIEW_CHARS`` with
      U+2026 if longer.

    CR F2 (PR #107): ``kind`` and ``content`` flow through
    :func:`_sanitize_for_terminal` BEFORE width calculation and
    truncation, so an ANSI-escape-laced content can't execute on
    stdout and a newline-laced kind can't break row alignment. Width
    math uses the sanitized values so column widths match what's
    rendered.
    """
    safe_kinds = [_sanitize_for_terminal(m.kind) for m in memories]
    kind_width = max(_KIND_MIN_WIDTH, max(len(k) for k in safe_kinds))
    header = f"{'id':>4}  {'created_at':<19}  {'kind':<{kind_width}}  content"
    lines = [header]
    for m, safe_kind in zip(memories, safe_kinds, strict=True):
        when = m.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
        safe_content = _sanitize_for_terminal(m.content)
        if len(safe_content) > _CONTENT_PREVIEW_CHARS:
            safe_content = safe_content[:_CONTENT_PREVIEW_CHARS] + _HORIZONTAL_ELLIPSIS
        lines.append(f"{m.id:>4}  {when:<19}  {safe_kind:<{kind_width}}  {safe_content}")
    return "\n".join(lines)


def _truncate_preview(text: str, max_chars: int = _FORGET_PREVIEW_CHARS) -> str:
    """One-line truncated preview for the ``forget`` confirmation prompt.

    CR F2 (PR #107): sanitize first via :func:`_sanitize_for_terminal`
    so terminal-unsafe characters (ANSI escapes, screen-clear sequences,
    OSC fake-prompt payloads) can't execute from a seeded memory's
    content when echoed back to the user. The sanitizer's ``Cc`` rule
    also replaces embedded newlines with ``?`` so the prompt stays on
    a single line — subsumes the pre-CR
    ``.replace("\\r\\n", " ")`` chain. Any future caller of
    :func:`_truncate_preview` gets the safety for free.
    """
    sanitized = _sanitize_for_terminal(text)
    if len(sanitized) <= max_chars:
        return sanitized
    return sanitized[:max_chars] + _HORIZONTAL_ELLIPSIS


def _stdin_is_tty() -> bool:
    """Module-indirect ``sys.stdin.isatty()`` so tests can monkeypatch cleanly.

    Monkeypatching ``sys.stdin.isatty`` directly is fragile under
    pytest's capture (``sys.stdin`` is a captured stream, not a
    terminal, and the method may not be present or settable). A
    module-local function sidesteps that: tests target
    ``agent.memory_cli._stdin_is_tty`` with a lambda.
    """
    return sys.stdin.isatty()


def _confirm_forget(memory: Memory, *, assume_yes: bool) -> bool:
    """Return True if the ``forget`` should proceed, False to abort.

    Three paths:

    * ``assume_yes`` (``--yes`` / ``-y``) → True without prompting.
    * stdin is not a TTY (piped input) → True without prompting. Unix
      pattern for ``rm -i`` behavior — piping always proceeds.
    * Interactive TTY → emit a one-line prompt and accept ``y`` / ``yes``
      (case-insensitive) as approval; anything else aborts.
    """
    if assume_yes:
        return True
    if not _stdin_is_tty():
        return True
    preview = _truncate_preview(memory.content)
    answer = input(f'Forget "{preview}"? [y/N] ').strip().lower()
    return answer in {"y", "yes"}


async def _write_jsonl_export(
    memories: list[Memory],
    path: Path,
    *,
    force: bool,
) -> None:
    """Atomically write ``memories`` as JSONL to ``path``.

    Raises:
        FileExistsError: ``path`` already exists and ``force`` is False —
            either caught at the :func:`_cmd_export` pre-check (fast
            user-friendly path) or at the no-clobber install step
            below (CR R2 F3's TOCTOU guard).
        OSError: any underlying filesystem error from mkstemp / write /
            fsync / install, including the no-hardlink-support
            ``errno.EPERM`` / ``EOPNOTSUPP`` / ``ENOSYS`` surfaced by
            :func:`os.link` on filesystems like FAT/exFAT.
        UnicodeError: a Memory's content or metadata holds a
            non-UTF-8-encodable value (defense-in-depth —
            :func:`agent.memory.add_memory` already guards at the
            write seam).

    Writes through a sibling tempfile in ``path.parent``, ``fsync``s,
    chmods to 0o600 on POSIX, then installs onto ``path`` with a
    platform-aware no-clobber step when ``force`` is False:

    * ``force=True``: :func:`os.replace` (unconditional overwrite —
      matches the user's explicit intent).
    * ``force=False`` on POSIX: :func:`os.link` (atomic — raises
      :class:`FileExistsError` if ``path`` exists at link time; does
      NOT remove the source entry, so ``finally`` cleans the tempfile).
    * ``force=False`` on Windows: :func:`os.rename` (Python 3.11+
      raises :class:`FileExistsError` on existing destination; moves
      the source so tempfile cleanup is a no-op via the
      ``FileNotFoundError`` suppression below).

    CR R2 F3 (PR #107): the pre-check in :func:`_cmd_export` closes
    the common case, and this no-clobber install step closes the
    TOCTOU window between that pre-check and here — another writer
    (second ``memory export`` call, concurrent FRIDAY process, user
    shell ``touch``) can race in between and silently get its content
    overwritten by an unconditional :func:`os.replace`. Mirrors
    :func:`agent.session.save_session`'s atomic-rename intent plus
    this extra no-clobber discipline.
    """
    # CR R2 F3: the pre-check for target-exists-without-force lives
    # at the caller (``_cmd_export``). The install step below is the
    # authoritative no-clobber barrier for the TOCTOU-race case and
    # relies on ``os.link`` / ``os.rename`` to raise FileExistsError
    # atomically, so there is nothing to pre-check here.

    header = {
        "schema_version": SCHEMA_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "count": len(memories),
    }

    fd, tmp_path = tempfile.mkstemp(
        prefix=".memory_export.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(header, ensure_ascii=False, separators=(",", ":")) + "\n")
            for memory in memories:
                f.write(
                    json.dumps(
                        _memory_to_dict(memory),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
            f.flush()
            os.fsync(f.fileno())
        if sys.platform != "win32":
            # mkstemp's own default is 0o600 on POSIX, but belt-and-
            # suspenders over a hostile umask matches session.py's
            # posture. Windows is a no-op (NTFS uses ACLs; the
            # agent/paths.py 0o700 parent dir carries the guarantee).
            os.chmod(tmp_path, 0o600)

        if force:
            # Unconditional overwrite per the user's --force intent.
            # os.replace removes the source entry on success, so the
            # finally-block unlink below hits FileNotFoundError and
            # suppresses — one cleanup site handles every path.
            os.replace(tmp_path, path)
        else:
            # No-clobber install: close the TOCTOU window that would
            # otherwise let a mid-write writer get silently overwritten
            # after the pre-check cleared.
            try:
                if sys.platform == "win32":
                    # 3.11+ Windows os.rename raises FileExistsError on
                    # existing destination (unlike POSIX rename which
                    # overwrites silently). Moves the source — no
                    # leftover tempfile.
                    os.rename(tmp_path, path)
                else:
                    # os.link is atomic and raises FileExistsError if
                    # path exists at link time. The source entry stays,
                    # so the finally-block cleanup unlinks it.
                    os.link(tmp_path, path)
            except FileExistsError as exc:
                # Race: another writer created `path` between the
                # pre-check in _cmd_export and here. Re-raise with a
                # message that points the user at --force; the outer
                # handler in _cmd_export surfaces it as "export failed:
                # ...".
                raise FileExistsError(
                    f"export target {path} was created mid-write; re-run with --force to overwrite."
                ) from exc
            except OSError as exc:
                # POSIX-only: hard links unsupported on the target
                # filesystem (FAT, exFAT, some network mounts). Surface
                # a clear remedy pointing at --force rather than
                # silently falling back to os.replace, which would
                # defeat the no-clobber invariant this branch exists
                # to uphold.
                if sys.platform != "win32" and exc.errno in (
                    errno.EPERM,
                    errno.EOPNOTSUPP,
                    errno.ENOSYS,
                ):
                    raise OSError(
                        f"destination filesystem does not support hard "
                        f"links (errno={exc.errno}); re-run with --force "
                        f"to use unconditional rename, or export to a "
                        f"different filesystem."
                    ) from exc
                raise
    finally:
        # Unconditional tempfile cleanup. Handles all three cases:
        #   os.replace success — tempfile already moved → FileNotFound
        #     → suppressed.
        #   os.link success — tempfile still on disk alongside target
        #     → unlinked here.
        #   Any exception path — tempfile may or may not exist →
        #     unlinked or FileNotFound-suppressed.
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_path)
