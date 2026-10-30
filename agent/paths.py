"""Platform-aware data directory for FRIDAY (ADR-0006).

Provides :func:`get_data_dir`, the single helper every Tier 4
persistence module (``session.json``, ``memory.db``) uses to
locate FRIDAY's on-disk state. The returned directory is created
with restrictive permissions (``0o700`` on POSIX) on first call
and reused idempotently on subsequent calls.

Path convention:

* Linux: ``$XDG_DATA_HOME/friday`` if the env var is set and
  non-empty, else ``~/.local/share/friday``.
* macOS: ``~/Library/Application Support/friday``.
* Windows: ``%APPDATA%\\friday`` if the env var is set, else
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
        xdg = os.environ.get("XDG_DATA_HOME", "").strip()
        base = Path(xdg) if xdg else Path.home() / ".local" / "share"
        data_dir = base / "friday"
    elif sys.platform == "darwin":
        data_dir = Path.home() / "Library" / "Application Support" / "friday"
    elif sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA", "").strip()
        if appdata:
            data_dir = Path(appdata) / "friday"
        else:
            data_dir = Path.home() / ".friday"
    else:
        data_dir = Path.home() / ".friday"

    try:
        data_dir.mkdir(parents=True, mode=0o700)
    except FileExistsError:
        # Already exists — don't re-chmod. A user may have
        # intentionally relaxed perms (e.g. a shared account);
        # idempotent reuse preserves that.
        pass
    return data_dir
