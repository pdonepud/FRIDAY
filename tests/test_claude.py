"""Tests for the thin seam over the Anthropic SDK (``agent.claude``)."""

from unittest.mock import MagicMock

import agent.claude


def test_get_client_is_lazy(monkeypatch):
    """The module-level client cache starts as ``None``.

    ``agent/claude.py`` builds the Anthropic client on first call to
    ``_get_client`` — not at import. This lets the smoke tests run
    without an ``ANTHROPIC_API_KEY`` set and keeps import-time side
    effects to zero (see AGENT.md's swap-friendly architecture).
    """
    # Reset in case a prior test populated the cache.
    monkeypatch.setattr(agent.claude, "_client", None)
    assert agent.claude._client is None


def test_get_client_caches_instance(monkeypatch):
    """Repeated ``_get_client()`` calls return the same client instance.

    Verifies the lazy-cache pattern actually caches — the Anthropic
    constructor is invoked once and subsequent calls return the same
    object.
    """
    monkeypatch.setattr(agent.claude, "_client", None)
    fake_client = MagicMock(name="Anthropic()")
    fake_ctor = MagicMock(return_value=fake_client)
    monkeypatch.setattr(agent.claude.anthropic, "Anthropic", fake_ctor)

    first = agent.claude._get_client()
    second = agent.claude._get_client()

    assert first is second is fake_client
    fake_ctor.assert_called_once()


def test_stream_reply_yields_text_chunks(mock_claude_client):
    """With the client mocked, ``stream_reply`` yields the chunks in order.

    Verifies the seam's contract: given a client whose
    ``messages.stream(...)`` context yields two text chunks, the
    seam surfaces the same two chunks to the caller.
    """
    ctx = MagicMock()
    ctx.__enter__.return_value.text_stream = iter(["hel", "lo"])
    ctx.__exit__.return_value = None
    mock_claude_client.messages.stream.return_value = ctx

    chunks = list(agent.claude.stream_reply([{"role": "user", "content": "hi"}], "sys"))

    assert chunks == ["hel", "lo"]
    mock_claude_client.messages.stream.assert_called_once()
