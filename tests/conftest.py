"""Shared pytest fixtures for the FRIDAY test suite."""

# ---------------------------------------------------------------------------
# Pin pynput's keyboard AND mouse backends to the dummy no-op implementation
# BEFORE any test module imports pynput. Headless CI runners (Ubuntu without
# DISPLAY) can't init the xorg backend, and pynput/__init__.py:42-43 does an
# unconditional `from . import keyboard` + `from . import mouse`, so BOTH env
# vars must be set or the mouse submodule still explodes at collection time.
# `setdefault` so a real backend on a developer machine wins.
# See CodeRabbit finding on PR #59.
# ---------------------------------------------------------------------------
import os

os.environ.setdefault("PYNPUT_BACKEND_KEYBOARD", "dummy")
os.environ.setdefault("PYNPUT_BACKEND_MOUSE", "dummy")

from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture
def mock_claude_client(monkeypatch):
    """Patch ``agent.claude._get_client`` to return a MagicMock.

    Use as ``mock_claude_client`` in a test signature to interact with
    the mocked Anthropic client. Configure its ``.messages.stream(...)``
    return value to drive ``agent.claude.stream_reply`` under test.
    """
    mock = MagicMock(name="anthropic.Anthropic()")
    monkeypatch.setattr("agent.claude._get_client", lambda: mock)
    return mock
