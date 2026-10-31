"""Tests for :func:`agent.paths.get_data_dir`.

Covers all four platform branches (Linux XDG / Linux fallback /
macOS / Windows APPDATA / Windows fallback / unknown) via
``sys.platform`` monkeypatching, plus idempotency and the
POSIX-only restrictive-permissions assertion.
"""

from __future__ import annotations

import stat
import sys

import pytest

from agent.paths import get_data_dir


def test_get_data_dir_linux_uses_xdg_data_home(monkeypatch, tmp_path):
    """Linux + ``$XDG_DATA_HOME`` set returns ``<xdg>/friday``."""
    xdg = tmp_path / "xdg"
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    result = get_data_dir()
    assert result == xdg / "friday"
    assert result.is_dir()


def test_get_data_dir_linux_fallback_without_xdg(monkeypatch, tmp_path):
    """Linux with ``$XDG_DATA_HOME`` unset falls back to
    ``~/.local/share/friday``."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    # pathlib.Path.home() reads USERPROFILE on Windows and HOME
    # elsewhere; set both so the test is host-OS-safe.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    result = get_data_dir()
    assert result == tmp_path / ".local" / "share" / "friday"
    assert result.is_dir()


def test_get_data_dir_macos(monkeypatch, tmp_path):
    """macOS returns ``~/Library/Application Support/friday``."""
    monkeypatch.setattr(sys, "platform", "darwin")
    # pathlib.Path.home() reads USERPROFILE on Windows and HOME
    # elsewhere; set both so the test is host-OS-safe.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    result = get_data_dir()
    assert result == tmp_path / "Library" / "Application Support" / "friday"
    assert result.is_dir()


def test_get_data_dir_windows_uses_appdata(monkeypatch, tmp_path):
    """Windows + ``%APPDATA%`` set returns ``<appdata>/friday``."""
    appdata = tmp_path / "appdata"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(appdata))
    result = get_data_dir()
    assert result == appdata / "friday"
    assert result.is_dir()


def test_get_data_dir_windows_fallback_without_appdata(monkeypatch, tmp_path):
    """Windows with ``%APPDATA%`` unset falls back to
    ``~/.friday``."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("APPDATA", raising=False)
    # On Windows, pathlib.Path.home() reads USERPROFILE; mock both
    # to be host-OS-safe (this test may run on actual Linux CI).
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    result = get_data_dir()
    assert result == tmp_path / ".friday"
    assert result.is_dir()


def test_get_data_dir_unknown_platform_fallback(monkeypatch, tmp_path):
    """Unrecognized ``sys.platform`` falls back to ``~/.friday``."""
    monkeypatch.setattr(sys, "platform", "freebsd13")
    # pathlib.Path.home() reads USERPROFILE on Windows and HOME
    # elsewhere; set both so the test is host-OS-safe.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    result = get_data_dir()
    assert result == tmp_path / ".friday"
    assert result.is_dir()


def test_get_data_dir_idempotent(monkeypatch, tmp_path):
    """Calling twice must not raise and both calls return the same
    path."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    first = get_data_dir()
    second = get_data_dir()
    assert first == second
    assert first.is_dir()


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="POSIX file-mode bits are not meaningful on Windows",
)
def test_get_data_dir_sets_restrictive_perms_on_posix(monkeypatch, tmp_path):
    """On POSIX, a newly-created data dir has mode ``0o700``
    (user-only read/write/execute)."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    result = get_data_dir()
    mode = stat.S_IMODE(result.stat().st_mode)
    assert mode == 0o700, f"expected 0o700, got {oct(mode)}"


def test_get_data_dir_raises_notadirectoryerror_when_target_is_a_file(monkeypatch, tmp_path):
    """#73 CR F1: if a regular file occupies the target path,
    get_data_dir() raises NotADirectoryError with the path in
    the message — not a silent-return-the-file-path path that
    would surface as a confusing error at the first consumer's
    compose site."""
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    target = xdg / "friday"
    target.write_text("stray file at the data-dir path")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))

    with pytest.raises(NotADirectoryError) as exc_info:
        get_data_dir()

    # Path appears in the message so the failure points at the
    # real cause, not a generic "it exists but isn't a dir".
    assert str(target) in str(exc_info.value)


def test_get_data_dir_linux_ignores_relative_xdg_data_home(monkeypatch, tmp_path):
    """#73 CR R2 F2: per the XDG Base Directory Spec, a relative
    ``$XDG_DATA_HOME`` must be ignored and the default used.
    Otherwise calls from different working directories would
    select different storage locations, breaking the
    single-data-directory guarantee."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", "relative/data/home")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    result = get_data_dir()
    assert result == tmp_path / ".local" / "share" / "friday"
    assert result.is_dir()


def test_get_data_dir_linux_preserves_whitespace_in_xdg_data_home(monkeypatch, tmp_path):
    """#73 CR R2 F1: an absolute XDG path with internal whitespace
    (POSIX allows this) must pass through to Path unchanged, not
    stripped."""
    xdg = tmp_path / "xdg with spaces"
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    result = get_data_dir()
    assert result == xdg / "friday"
    assert result.is_dir()


def test_get_data_dir_linux_whitespace_only_xdg_falls_back(monkeypatch, tmp_path):
    """#73 CR R2 F1: whitespace-only ``$XDG_DATA_HOME`` is treated
    as unset — the empty-vs-whitespace distinction uses
    ``.strip()`` for the is-set check without stripping the actual
    value passed to Path."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", "   ")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    result = get_data_dir()
    assert result == tmp_path / ".local" / "share" / "friday"
    assert result.is_dir()


def test_get_data_dir_windows_ignores_relative_appdata(monkeypatch, tmp_path):
    """#73 CR R2: Windows APPDATA branch gets the same relative-
    rejection treatment as the Linux XDG branch for symmetry —
    a relative APPDATA (unusual in practice but possible from a
    shell-constructed env) must fall back rather than select a
    CWD-relative location."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", "relative\\appdata")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    result = get_data_dir()
    assert result == tmp_path / ".friday"
    assert result.is_dir()


def test_get_data_dir_windows_rejects_drive_relative_appdata(monkeypatch, tmp_path):
    """#73 CR R3 F1: on Windows, ``os.path.isabs(r"\\data")``
    returns True on Python versions before 3.11.10 / 3.12.4,
    incorrectly accepting a drive-relative path whose resolved
    location depends on the current drive. ``Path.is_absolute()``
    correctly rejects such paths — a drive-relative APPDATA must
    fall back to ``~/.friday``."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", r"\data")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    result = get_data_dir()
    assert result == tmp_path / ".friday"
    assert result.is_dir()
