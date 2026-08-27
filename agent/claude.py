"""Thin seam over the Anthropic SDK.

Per AGENT.md's swap-friendly architecture: this is the ONLY module in the
codebase that imports `anthropic`. Everything else goes through
`stream_reply` and the re-exported exception classes.

The client is constructed lazily on first use so importing this module has
no side effects and does not require ANTHROPIC_API_KEY to be set — the
smoke test in agent/tests depends on that.
"""

from collections.abc import Iterator

import anthropic
from anthropic import APIConnectionError, AuthenticationError, RateLimitError

from agent.models import MODEL

__all__ = [
    "stream_reply",
    "APIConnectionError",
    "AuthenticationError",
    "RateLimitError",
]

_MAX_TOKENS: int = 1024

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    """Return the shared Anthropic client, constructing it on first use.

    Lazy so that importing this module does not read ANTHROPIC_API_KEY.
    Callers must have loaded the environment (e.g. via python-dotenv)
    before the first invocation.
    """
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def stream_reply(messages: list[dict], system: str) -> Iterator[str]:
    """Stream Claude's reply, yielding text chunks as they arrive.

    Args:
        messages: full conversation so far, in Anthropic message format
            (list of {"role": "user"|"assistant", "content": str}).
        system: system prompt string.

    Yields:
        Text chunks in arrival order. Concatenating every chunk gives
        the complete reply.

    Raises:
        anthropic.AuthenticationError: bad or missing API key.
        anthropic.RateLimitError: quota exhausted.
        anthropic.APIConnectionError: network problem reaching the API.
        anthropic.APIError: other API-side failures.
    """
    client = _get_client()
    with client.messages.stream(
        model=MODEL,
        max_tokens=_MAX_TOKENS,
        system=system,
        messages=messages,
    ) as stream:
        for text in stream.text_stream:
            yield text
