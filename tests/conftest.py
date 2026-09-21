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


@pytest.fixture(autouse=True)
def _reset_claude_client_cache(monkeypatch):
    """Reset ``agent.claude._client`` before every test.

    ``AsyncAnthropic``'s underlying ``httpx2.AsyncClient`` binds to the
    asyncio event loop it's first used in. pytest-asyncio auto-mode
    (see ``pyproject.toml:29``) runs each ``async def test_*`` under a
    fresh ``asyncio.run()``; a cached instance from a prior test would
    be a landmine for cross-test flakiness.

    Autouse for defense-in-depth: every test starts with ``_client =
    None`` regardless of whether it touches ``agent.claude`` directly.
    """
    monkeypatch.setattr("agent.claude._client", None)


@pytest.fixture
def mock_claude_client(monkeypatch):
    """Patch ``agent.claude._get_client`` to return an ``AsyncAnthropic`` mock.

    Use as ``mock_claude_client`` in a test signature to interact with
    the mocked ``AsyncAnthropic`` client. Configure its
    ``.messages.stream(...)`` return value to drive
    ``agent.claude.stream_tokens`` under test.

    The returned mock is a top-level ``MagicMock``; the async surface
    is only at ``client.messages.stream(...)`` return value, which
    tests wire per-test as an async context manager whose
    ``__aenter__`` yields an object with an async-iterator
    ``text_stream``. This mirrors the fake-context-manager pattern in
    ``tests/test_tts.py``.
    """
    mock = MagicMock(name="AsyncAnthropic()")
    monkeypatch.setattr("agent.claude._get_client", lambda: mock)
    return mock
