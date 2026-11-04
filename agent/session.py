"""Session persistence for :class:`agent.buffer.ConversationBuffer` (ADR-0006 §Short-term).

ADR-0006 specifies that the short-term conversation buffer is
"serialized to ``<data_dir>/session.json`` on clean shutdown and
``atexit``. Loaded on startup. Atomic write via temp file + ``os.replace``.
On corrupt or missing file, log a warning and start empty." This module
is the sole owner of that JSON format and the atomic write path.

On-disk schema (version 1):

.. code-block:: json

    {
      "version": 1,
      "turns": [
        {"role": "user", "content": "...", "timestamp": "2026-11-03T14:02:11.123456+00:00"}
      ]
    }

``timestamp`` round-trips via :meth:`datetime.datetime.isoformat` /
:meth:`datetime.datetime.fromisoformat`, which preserves tz-aware
offsets losslessly on Python 3.11+. :class:`agent.buffer.Turn`'s own
``__post_init__`` re-asserts the tz-aware invariant at construction, so
any stripped-tzinfo payload fails loudly on load rather than at a later
comparison site.

Durability posture: ``os.replace`` is atomic on the same filesystem on
POSIX and atomic-rename on Windows. Full crash / power-loss recovery is
explicitly out of scope per the issue — a torn write on power loss
surfaces as a parse error on the next load, which the "discard if
corrupt" branch handles cleanly.

Not thread-safe. FRIDAY's asyncio-single-threaded model is assumed
(mirrors ``agent/buffer.py``); adding threading support would be a
breaking change tracked at that point.

Scope posture (Option 1, per the #75 plan): this module ships the
save/load machinery and an ``atexit`` helper. Loop wiring (``voice_loop``
and ``text_loop`` calling :func:`load_session` at startup and
:func:`register_shutdown_save` at buffer construction) is deferred — the
loops still use ``messages: list[dict]`` rather than
:class:`ConversationBuffer`, so that adoption properly lands with #79
(context assembly) or a dedicated follow-up.

Public surface:
    SESSION_FILENAME, SCHEMA_VERSION — module constants.
    default_session_path() — ``<data_dir>/session.json``.
    save_session(buffer, path) — atomic JSON write.
    load_session(path, turn_limit) — reconstruct buffer; corrupt → empty.
    register_shutdown_save(buffer, path) — idempotent atexit hook.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from agent.buffer import _BUFFER_TURN_LIMIT, ConversationBuffer, Turn
from agent.paths import get_data_dir

#: Filename under ``<data_dir>`` where the serialized buffer lives.
SESSION_FILENAME: Final[str] = "session.json"

#: On-disk schema version. Bumped when the JSON shape changes in a way
#: that old loaders would mis-read; each bump introduces a corresponding
#: load-time compatibility branch or a deliberate "discard pre-vN files"
#: decision.
SCHEMA_VERSION: Final[int] = 1

_log = logging.getLogger("agent.session")

# Idempotency guard for :func:`register_shutdown_save`. Keyed by
# ``(id(buffer), str(path))`` so a caller re-registering the same pair
# is a no-op. Not thread-safe; see module docstring.
_registered: set[tuple[int, str]] = set()


def default_session_path() -> Path:
    """Return ``<data_dir>/session.json`` (platform-aware).

    Resolves via :func:`agent.paths.get_data_dir`, so the first call
    bootstraps the data directory with the restrictive 0o700 mode on
    POSIX. No I/O beyond that side effect.
    """
    return get_data_dir() / SESSION_FILENAME


def save_session(buffer: ConversationBuffer, path: Path) -> None:
    """Atomically write ``buffer`` to ``path`` as schema-v1 JSON.

    Writes to a sibling temp file in ``path.parent``, ``fsync``s, then
    ``os.replace``s onto ``path``. On POSIX the resulting file is mode
    ``0o600``; on Windows the parent directory's ACL (from
    :mod:`agent.paths`) governs.

    Failures are logged at WARNING and swallowed — this function is
    called from ``atexit`` and must never crash interpreter shutdown.
    """
    payload: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "turns": [
            {
                "role": t.role,
                "content": t.content,
                "timestamp": t.timestamp.isoformat(),
            }
            for t in buffer
        ],
    }

    tmp_path: str | None = None
    replaced = False
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=".session.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        tmp_path = tmp_name
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        if sys.platform != "win32":
            # Belt-and-suspenders over mkstemp's default 0o600. On
            # Windows chmod with mode bits is a no-op (NTFS uses ACLs);
            # the parent directory from agent/paths.py carries the
            # permission guarantee there.
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        replaced = True
    except OSError as exc:
        _log.warning("session save failed at %s: %s", path, exc)
    finally:
        if tmp_path is not None and not replaced:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                pass  # best-effort cleanup — we already failed above


def load_session(
    path: Path,
    turn_limit: int = _BUFFER_TURN_LIMIT,
) -> ConversationBuffer:
    """Reconstruct a :class:`ConversationBuffer` from ``path``.

    Missing file → empty buffer, silent (expected on first run).
    Any corruption (JSON error, wrong schema version, missing/wrong
    fields, invalid turn) → WARNING + empty buffer, matching the ADR's
    "discard if corrupt" posture.

    ``turn_limit`` controls the returned buffer's FIFO ceiling, not the
    saved file. If the saved turn count exceeds ``turn_limit``, the
    deque's ``maxlen`` evicts oldest first — the current run's limit
    wins, preserving the most recent turns.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ConversationBuffer(turn_limit=turn_limit)
    except OSError as exc:
        _log.warning("session load read failed at %s: %s", path, exc)
        return ConversationBuffer(turn_limit=turn_limit)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        _log.warning("session load corrupt at %s: %s", path, exc)
        return ConversationBuffer(turn_limit=turn_limit)

    if not isinstance(payload, dict):
        _log.warning("session load corrupt at %s: top-level is not an object", path)
        return ConversationBuffer(turn_limit=turn_limit)
    if payload.get("version") != SCHEMA_VERSION:
        _log.warning(
            "session load unknown version at %s: got %r, expected %d",
            path,
            payload.get("version"),
            SCHEMA_VERSION,
        )
        return ConversationBuffer(turn_limit=turn_limit)
    turns_raw = payload.get("turns")
    if not isinstance(turns_raw, list):
        _log.warning("session load corrupt at %s: 'turns' is not a list", path)
        return ConversationBuffer(turn_limit=turn_limit)

    reconstructed: list[Turn] = []
    for d in turns_raw:
        try:
            reconstructed.append(
                Turn(
                    role=d["role"],
                    content=d["content"],
                    timestamp=datetime.fromisoformat(d["timestamp"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            # Discard whole file on any per-turn failure — simpler than
            # skip-bad-turn-keep-rest, and matches "discard if corrupt".
            reason = str(exc)
            if len(reason) > 200:
                reason = reason[:200] + "..."
            _log.warning("session load bad turn at %s: %s", path, reason)
            return ConversationBuffer(turn_limit=turn_limit)

    buf = ConversationBuffer(turn_limit=turn_limit)
    for t in reconstructed:
        buf.append(t)
    return buf


def _run_save(buffer: ConversationBuffer, path: Path) -> None:
    """Internal atexit callback — wraps :func:`save_session` safely.

    An ``atexit`` callback must never raise (an exception there dumps a
    traceback to stderr during interpreter shutdown and masks any
    earlier failure). ``save_session`` already swallows ``OSError``, but
    this outer guard catches anything surprising (serialization
    exceptions on an exotic Turn subclass, etc.).
    """
    try:
        save_session(buffer, path)
    except Exception as exc:  # noqa: BLE001 — atexit must never raise
        _log.warning("session atexit save failed: %s", exc)


def register_shutdown_save(buffer: ConversationBuffer, path: Path) -> None:
    """Register an ``atexit`` callback to persist ``buffer`` to ``path``.

    Idempotent per ``(id(buffer), str(path))`` — repeated calls with the
    same pair register exactly one callback. The callback fires on
    normal interpreter exit (including ``sys.exit`` and uncaught
    exceptions propagating to the top level). It does **not** fire on
    ``os._exit`` or SIGKILL.
    """
    key = (id(buffer), str(path))
    if key in _registered:
        return
    _registered.add(key)
    atexit.register(_run_save, buffer, path)
