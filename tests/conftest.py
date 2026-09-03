"""Shared pytest fixtures for the FRIDAY test suite."""

from unittest.mock import MagicMock

import pytest


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
