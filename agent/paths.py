"""Platform-aware data directory for FRIDAY (ADR-0006).

Provides :func:`get_data_dir`, the single helper every Tier 4
persistence module (``session.json``, ``memory.db``) uses to
locate FRIDAY's on-disk state. The returned directory is created
with restrictive permissions (``0o700`` on POSIX) on first call
and reused idempotently on subsequent calls.

Path convention:

* Linux: ``$XDG_DATA_HOME/friday`` if the env var is set to an
  absolute path, else ``~/.local/share/friday`` (per the XDG
  Base Directory Spec, relative values are ignored).
* macOS: ``~/Library/Application Support/friday``.
* Windows: ``%APPDATA%\\friday`` if the env var is set to an
  absolute path (checked via ``Path.is_absolute()``, which
  rejects drive-relative values like ``\\data``), else
  ``~/.friday``.
* Unknown platform: ``~/.friday``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def get_data_dir() -> Path:
    """Return FRIDAY's platform-appropriate data directory.

    Creates the directory on first call with mode ``0o700`` on
    POSIX systems (user-only read/write/execute). On subsequent
    calls, the directory already exists and permissions are left
    untouched — a user's intentional relax (e.g. ``0o750`` for a
    shared system account) survives repeated calls.

    Returns:
        pathlib.Path: the data directory, guaranteed to exist and
            be a directory after this call returns.
    """
    if sys.platform.startswith("linux"):
        # #73 CR R2:
        # F1 — ``.strip()`` on the env value mangles paths with
        #   legitimate leading/trailing whitespace (POSIX allows
        #   almost any character in filenames). Trim ONLY for the
        #   is-set check; pass the raw value through to Path.
        # F2 — the XDG Base Directory Spec requires relative values
        #   to be ignored and the default used instead (see
        #   https://specifications.freedesktop.org/basedir-spec/latest/).
        #   Without an absolute-path check, calls from different
        #   working directories would select different storage
        #   locations, breaking the single-data-directory guarantee.
        # CR R3 F1: switched from ``os.path.isabs`` to
        # ``Path.is_absolute()`` because the former returns True for
        # drive-relative Windows paths (``\data``) on Python versions
        # before 3.11.10 / 3.12.4, which defeats the single-storage-
        # location guarantee for launches from different drives. The
        # pathlib check correctly rejects such paths regardless of
        # Python version.
        xdg = os.environ.get("XDG_DATA_HOME", "")
        if xdg.strip() and Path(xdg).is_absolute():
            base = Path(xdg)
        else:
            base = Path.home() / ".local" / "share"
        data_dir = base / "friday"
    elif sys.platform == "darwin":
        data_dir = Path.home() / "Library" / "Application Support" / "friday"
    elif sys.platform.startswith("win"):
        # #73 CR R2: same F1 + F2 treatment as the Linux branch
        # above. APPDATA is not XDG-governed and is almost always
        # set by Windows to an absolute path, but the same two
        # correctness concerns apply in theory — a user with a
        # whitespace-prefixed APPDATA or a shell-constructed
        # relative APPDATA would otherwise silently select a
        # non-canonical storage location. Mirror the Linux
        # treatment for consistency.
        # CR R3 F1: switched from ``os.path.isabs`` to
        # ``Path.is_absolute()`` because the former returns True for
        # drive-relative Windows paths (``\data``) on Python versions
        # before 3.11.10 / 3.12.4, which defeats the single-storage-
        # location guarantee for launches from different drives. The
        # pathlib check correctly rejects such paths regardless of
        # Python version.
        appdata = os.environ.get("APPDATA", "")
        if appdata.strip() and Path(appdata).is_absolute():
            data_dir = Path(appdata) / "friday"
        else:
            data_dir = Path.home() / ".friday"
    else:
        data_dir = Path.home() / ".friday"

    try:
        data_dir.mkdir(parents=True, mode=0o700)
    except FileExistsError:
        # #73 CR F1: FileExistsError without exist_ok=True fires for
        # both "directory already exists" AND "regular file occupies
        # this path". Only the first case is legitimate idempotent
        # reuse; the second violates the docstring's "guaranteed to
        # be a directory" promise and would surface as a confusing
        # NotADirectoryError at the first consumer's compose site
        # (e.g. ``data_dir / "session.json"`` in #75). Raise
        # explicitly here, with the path in the message, so the
        # failure points at the real cause.
        if not data_dir.is_dir():
            raise NotADirectoryError(f"{data_dir} exists but is not a directory") from None
        # Directory already exists — don't re-chmod. A user may have
        # intentionally relaxed perms (e.g. a shared account);
        # idempotent reuse preserves that.
    return data_dir
