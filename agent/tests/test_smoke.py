"""Import-only smoke test — no network, no API key required.

Confirms the agent package assembles and the wiring between modules is
intact. If this fails, the loop cannot start.
"""


def test_imports_and_wiring():
    """Import every agent module and assert the wiring between them is intact.

    Confirms the pinned model constant, the hardcoded user name, verbatim
    personality markers in the system prompt, and that the Anthropic
    exception classes are re-exported through the thin seam.
    """
    from agent import claude, loop, models, system_prompt

    # Model is set and matches the pinned Tier 1 choice.
    assert models.MODEL == "claude-sonnet-4-6"

    # User name is hardcoded per Tier 1 decision.
    assert system_prompt.USER_NAME == "Preetam"

    # System prompt actually references the user.
    assert "Preetam" in system_prompt.SYSTEM_PROMPT

    # Personality markers made it into the assembled prompt.
    assert "Playful, British, gently dry" in system_prompt.SYSTEM_PROMPT
    assert "once per session" in system_prompt.SYSTEM_PROMPT

    # Public seam functions exist and are callable.
    assert callable(claude.stream_reply)
    assert callable(loop.run)

    # Anthropic exception classes are re-exported so loop.py can catch
    # them without importing anthropic directly.
    assert claude.AuthenticationError is not None
    assert claude.RateLimitError is not None
    assert claude.APIConnectionError is not None
