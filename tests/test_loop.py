"""Smoke test for the conversation loop entry point (``agent.loop``).

Deliberately narrow: verify the loop module imports without side effects
and that ``run()`` handles the two boundary conditions of "start" and
"clean exit". Per the Tier 2 spec, this does **not** attempt to test the
interactive REPL end-to-end — deeper coverage lands tier-by-tier.
"""

from unittest.mock import MagicMock


def test_loop_and_main_import_without_side_effects():
    """Importing ``agent.loop`` and ``agent.__main__`` does not launch the REPL.

    The modules define constants and functions at import time; ``run()``
    is only invoked from ``agent.__main__`` under the ``if __name__ ==
    '__main__'`` guard. A bare import of either must never prompt or
    connect.
    """
    import agent.__main__ as agent_main
    import agent.loop

    assert callable(agent.loop.run)
    assert agent_main.run is agent.loop.run


def test_run_exits_one_when_api_key_missing(monkeypatch, capsys):
    """``run()`` returns exit code 1 with a friendly message when the API key is unset.

    Guards the loop's startup path: with no ``ANTHROPIC_API_KEY`` in the
    environment and ``load_dotenv`` stubbed so a local ``.env`` cannot
    repopulate it, ``run()`` must fail fast rather than proceeding into
    a call that would fail deeper.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)

    from agent.loop import run

    assert run() == 1
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().out


def test_run_exits_zero_on_ctrl_c_at_prompt(monkeypatch, capsys):
    """``run()`` returns exit code 0 when the user Ctrl+Cs at the prompt.

    Guards the loop's clean-exit path: with a dummy API key set and
    ``input()`` raising ``KeyboardInterrupt`` on the first call, the
    loop should print the goodbye line and return 0.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-dummy")
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)
    monkeypatch.setattr("builtins.input", MagicMock(side_effect=KeyboardInterrupt))

    from agent.loop import run

    assert run() == 0
    assert "goodbye" in capsys.readouterr().out
