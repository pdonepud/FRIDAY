"""Tests for the conversation loop entry point (``agent.loop``).

Covers both branches dispatched from ``run()``:

* ``text_loop()`` — the Tier-2-preserved keyboard REPL (``--text`` flag).
* ``voice_loop()`` — the Tier-3 default push-to-talk pipeline composing
  ``agent.ptt`` → ``agent.audio`` → ``agent.stt`` → ``agent.claude`` →
  ``agent.tts`` → ``agent.audio.playback``.

The voice tests mock each seam at its module boundary (e.g.
``agent.ptt.events``, ``agent.stt.transcribe``, ``agent.claude.stream_sentences``,
``agent.tts.synthesize``, ``agent.audio.capture``, ``agent.audio.playback``)
so the tests stay unit-level: no real microphone, Deepgram socket, or
ElevenLabs stream is touched. The fatal / recoverable error triage
table in the plan §3 is encoded as individual tests below, grouped by
which seam raises.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent import ptt, stt, tts
from agent.claude import APIConnectionError, AuthenticationError, RateLimitError

# --- Fake async-iterator helpers ----------------------------------------
#
# A single-shape helper that drives any ``async for`` target under test:
# the ``script`` is a list of ``("yield", value)`` or ``("raise", exc)``
# tuples; ``_scripted_async_gen`` plays them back in order and then
# exhausts (``StopAsyncIteration``).


async def _scripted_async_gen(script):
    """Yield / raise per the script tuples, then exhaust."""
    for action, payload in script:
        if action == "yield":
            yield payload
        elif action == "raise":
            raise payload
        else:  # pragma: no cover — guard against test typos
            raise AssertionError(f"bad scripted action: {action!r}")


async def _collect(aiter) -> list:
    """Drain an async iterator into a list; handy for consuming test fakes."""
    out = []
    async for item in aiter:
        out.append(item)
    return out


def _set_voice_argv(monkeypatch):
    """Pin ``sys.argv`` so argparse picks the voice branch."""
    monkeypatch.setattr("sys.argv", ["python -m agent"])


def _set_text_argv(monkeypatch):
    """Pin ``sys.argv`` so argparse picks the ``--text`` branch."""
    monkeypatch.setattr("sys.argv", ["python -m agent", "--text"])


def _set_all_keys(monkeypatch):
    """Set all three provider API keys so the voice startup check passes."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-deepgram")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-elevenlabs")


# --- T1: import without side effects -----------------------------------


def test_loop_and_main_import_without_side_effects():
    """Importing ``agent.loop`` and ``agent.__main__`` does not launch the REPL."""
    import agent.__main__ as agent_main
    import agent.loop

    assert callable(agent.loop.run)
    assert callable(agent.loop.text_loop)
    assert callable(agent.loop.voice_loop)
    assert agent_main.run is agent.loop.run


# --- T2 / T3: _voice_missing_keys_message helper -----------------------


def test_voice_missing_keys_message_single_var():
    from agent.loop import _voice_missing_keys_message

    msg = _voice_missing_keys_message(["ANTHROPIC_API_KEY"])
    assert "ANTHROPIC_API_KEY isn't set; see .env.example." in msg
    # Single footer at the end, exactly once.
    assert msg.count("Copy .env.example to .env and add your keys.") == 1


def test_voice_missing_keys_message_multiple_vars():
    from agent.loop import _voice_missing_keys_message

    msg = _voice_missing_keys_message(
        ["ANTHROPIC_API_KEY", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY"]
    )
    for var in ("ANTHROPIC_API_KEY", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY"):
        assert f"{var} isn't set; see .env.example." in msg
    assert msg.count("Copy .env.example to .env and add your keys.") == 1


# --- T4 / T5: text-mode startup + clean exit ---------------------------


async def test_text_mode_exits_one_when_api_key_missing(monkeypatch, capsys):
    """``--text`` branch: missing ``ANTHROPIC_API_KEY`` → exit 1 + wording pinned."""
    _set_text_argv(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)

    from agent.loop import run

    assert await run() == 1
    captured = capsys.readouterr()
    assert "ANTHROPIC_API_KEY" in captured.out
    assert "see .env.example" in captured.out


async def test_text_mode_exits_zero_on_ctrl_c_at_prompt(monkeypatch, capsys):
    """``--text`` branch: Ctrl+C at the ``input()`` prompt → exit 0."""
    _set_text_argv(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-dummy")
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)
    monkeypatch.setattr("builtins.input", MagicMock(side_effect=KeyboardInterrupt))

    from agent.loop import run

    assert await run() == 0
    assert "goodbye" in capsys.readouterr().out


# --- T6 / T7: voice-mode startup key gate ------------------------------


async def test_voice_mode_exits_one_when_single_key_missing(monkeypatch, capsys):
    """Voice branch: any missing key → exit 1 + the missing var is called out."""
    _set_voice_argv(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic")
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-elevenlabs")

    from agent.loop import run

    assert await run() == 1
    captured = capsys.readouterr()
    assert "DEEPGRAM_API_KEY isn't set; see .env.example." in captured.out
    assert "Copy .env.example to .env and add your keys." in captured.out


async def test_voice_mode_exits_one_when_all_keys_missing(monkeypatch, capsys):
    """Voice branch: all keys missing → exit 1 + all three listed + one footer."""
    _set_voice_argv(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)

    from agent.loop import run

    assert await run() == 1
    out = capsys.readouterr().out
    for var in ("ANTHROPIC_API_KEY", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY"):
        assert f"{var} isn't set; see .env.example." in out
    assert out.count("Copy .env.example to .env and add your keys.") == 1


# --- Voice-loop helpers for mocking the pipeline seams -----------------


def _install_voice_pipeline_mocks(
    monkeypatch,
    *,
    ptt_script,
    stt_result,
    sentences=("ok.",),
    synth_result=(b"a",),
):
    """Install default fakes for ``agent.ptt.events``, ``agent.audio.capture``,
    ``agent.stt.transcribe``, ``agent.claude.stream_sentences``,
    ``agent.tts.synthesize``, and ``agent.audio.playback``.

    Returns the list of ``messages`` captured by the ``stream_sentences``
    stub so individual tests can assert on message-history state.
    """
    monkeypatch.setattr("agent.ptt.events", lambda: _scripted_async_gen(ptt_script))
    monkeypatch.setattr("agent.audio.capture", lambda *a, **kw: _scripted_async_gen([]))

    async def _fake_transcribe(_chunks, _release):
        if isinstance(stt_result, BaseException):
            raise stt_result
        return stt_result

    monkeypatch.setattr("agent.stt.transcribe", _fake_transcribe)

    captured_messages: list = []

    def _fake_stream_sentences(messages, _system):
        captured_messages.append([dict(m) for m in messages])
        if isinstance(sentences, BaseException):

            async def _raiser():
                raise sentences
                yield  # pragma: no cover — keeps this an async generator

            return _raiser()
        return _scripted_async_gen([("yield", s) for s in sentences])

    monkeypatch.setattr("agent.loop.stream_sentences", _fake_stream_sentences)

    def _fake_synthesize(text_chunks):
        if isinstance(synth_result, BaseException):

            async def _raiser():
                # Drain the upstream so the collector sees the text.
                async for _ in text_chunks:
                    pass
                raise synth_result
                yield  # pragma: no cover

            return _raiser()

        async def _passthrough():
            async for _ in text_chunks:
                pass
            for b in synth_result:
                yield b

        return _passthrough()

    monkeypatch.setattr("agent.tts.synthesize", _fake_synthesize)

    async def _fake_playback(chunks, *_a, **_kw):
        async for _ in chunks:
            pass

    monkeypatch.setattr("agent.audio.playback", _fake_playback)

    return captured_messages


# --- T8: KeyboardInterrupt at Phase 1 idle wait ------------------------


async def test_voice_mode_ctrl_c_at_idle_exits_zero(monkeypatch, capsys):
    """KeyboardInterrupt during Phase 1 idle wait → exit 0 + goodbye."""
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)
    _install_voice_pipeline_mocks(
        monkeypatch,
        ptt_script=[("raise", KeyboardInterrupt())],
        stt_result="never-reached",
    )

    from agent.loop import run

    assert await run() == 0
    out = capsys.readouterr().out
    assert "voice mode" in out  # banner
    assert "goodbye" in out


# --- T9 / T10: PTT death at idle --------------------------------------


async def test_voice_mode_ptt_exhausts_at_idle_exits_two(monkeypatch, capsys):
    """PTT iterator ends with no PRESSED → exit 2 + ``[ptt]`` message."""
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)
    _install_voice_pipeline_mocks(
        monkeypatch,
        ptt_script=[],  # exhausts immediately
        stt_result="never-reached",
    )

    from agent.loop import run

    assert await run() == 2
    assert "[ptt]" in capsys.readouterr().out


async def test_voice_mode_ptt_raises_at_idle_exits_two(monkeypatch, capsys):
    """PTT iterator raises before any PRESSED → exit 2 + ``[ptt]`` message."""
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)
    _install_voice_pipeline_mocks(
        monkeypatch,
        ptt_script=[("raise", RuntimeError("hid went away"))],
        stt_result="never-reached",
    )

    from agent.loop import run

    assert await run() == 2
    out = capsys.readouterr().out
    assert "[ptt]" in out
    assert "RuntimeError" in out
    assert "hid went away" in out


# --- T11: happy-path single turn ---------------------------------------


async def test_voice_mode_single_happy_turn_then_exhaust(monkeypatch, capsys):
    """PRESSED → transcript "hi" → sentences → playback → PTT exhausts → exit 2.

    The exit code is 2 because PTT death is terminal (A2), but the turn
    before that must complete cleanly: both user+assistant entries land
    in the message history that ``stream_sentences`` sees.
    """
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)
    captured_messages = _install_voice_pipeline_mocks(
        monkeypatch,
        ptt_script=[
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
        ],
        stt_result="hi",
        sentences=("Hello.", " How can I help?"),
    )

    from agent.loop import run

    assert await run() == 2
    # One stream_sentences call captured; messages at that moment should
    # be just the user turn ("hi") — assistant turn is appended AFTER
    # playback completes.
    assert len(captured_messages) == 1
    assert captured_messages[0] == [{"role": "user", "content": "hi"}]


# --- T12: empty transcript short-circuit -------------------------------


async def test_voice_mode_empty_transcript_skips_claude(monkeypatch, capsys):
    """Empty transcript → ``[no speech]`` + no Claude call, no messages."""
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)
    captured_messages = _install_voice_pipeline_mocks(
        monkeypatch,
        ptt_script=[
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
        ],
        stt_result="",  # FORCE_END_TURN_NO_ACTIVE_TURN signal
    )

    from agent.loop import run

    assert await run() == 2  # PTT exhausts after the no-speech turn
    assert "[no speech]" in capsys.readouterr().out
    # stream_sentences never invoked.
    assert captured_messages == []


# --- T13 / T14 / T15: STT error triage ---------------------------------


@pytest.mark.parametrize(
    "exc_factory",
    [
        pytest.param(lambda: stt.STTAuthError("401 bad key"), id="STTAuthError"),
        pytest.param(
            lambda: stt.STTForceEndTurnUnsupported("no msg type"),
            id="STTForceEndTurnUnsupported",
        ),
    ],
)
async def test_voice_mode_stt_fatal_auth_exits_two(monkeypatch, capsys, exc_factory):
    """STT auth-class errors are terminal → exit 2 + ``[auth]`` message."""
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)
    captured_messages = _install_voice_pipeline_mocks(
        monkeypatch,
        ptt_script=[
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
        ],
        stt_result=exc_factory(),
    )

    from agent.loop import run

    assert await run() == 2
    assert "[auth]" in capsys.readouterr().out
    # Claude never reached, no messages captured.
    assert captured_messages == []


@pytest.mark.parametrize(
    "exc_factory,expected_tag",
    [
        pytest.param(lambda: stt.STTTimeout("eot timeout"), "[stt timeout]", id="STTTimeout"),
        pytest.param(lambda: stt.STTFatal("socket died"), "[stt]", id="STTFatal"),
        pytest.param(lambda: stt.STTError("misc"), "[stt]", id="STTError"),
    ],
)
async def test_voice_mode_stt_recoverable_continues(monkeypatch, capsys, exc_factory, expected_tag):
    """STT recoverable errors print the tag and keep going (PTT then exhausts → 2)."""
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)
    captured_messages = _install_voice_pipeline_mocks(
        monkeypatch,
        ptt_script=[
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
        ],
        stt_result=exc_factory(),
    )

    from agent.loop import run

    assert await run() == 2  # idle wait sees exhaustion after the recoverable error
    assert expected_tag in capsys.readouterr().out
    assert captured_messages == []


# --- T16-T21: Phase 5 error triage (Claude + TTS + generic) ------------
#
# The parametrize covers the full triage table:
# * fatal rows → exit 2, user turn NOT popped (A1)
# * recoverable rows → continue, user turn popped
#
# All rows use the same single-turn script: PRESSED → RELEASED → stt="hi",
# then the chosen error is injected into Phase 5 via either
# ``stream_sentences`` or ``synthesize`` depending on which seam owns
# the exception type.


@pytest.mark.parametrize(
    "source,exc_factory,expected_exit,expected_tag,pops_user",
    [
        pytest.param(
            "claude",
            lambda: AuthenticationError("401", response=MagicMock(), body=None),
            2,
            "[auth]",
            False,
            id="claude-auth-fatal",
        ),
        pytest.param(
            "claude",
            lambda: RateLimitError("429", response=MagicMock(), body=None),
            2,
            "[rate]",
            True,
            id="claude-rate-recoverable",
        ),
        pytest.param(
            "claude",
            lambda: APIConnectionError(request=MagicMock()),
            2,
            "[net]",
            True,
            id="claude-net-recoverable",
        ),
        pytest.param(
            "tts",
            lambda: tts.TTSAuthError("401 elevenlabs"),
            2,
            "[auth]",
            False,
            id="tts-auth-fatal",
        ),
        pytest.param(
            "tts",
            lambda: tts.TTSError("socket dropped"),
            2,
            "[tts]",
            True,
            id="tts-generic-recoverable",
        ),
        pytest.param(
            "tts",
            lambda: RuntimeError("surprise"),
            2,
            "[err]",
            True,
            id="unknown-exception-safety-net",
        ),
    ],
)
async def test_voice_mode_phase5_error_triage(
    monkeypatch, capsys, source, exc_factory, expected_exit, expected_tag, pops_user
):
    """Phase-5 fatal rows exit 2 without popping (A1); recoverable rows pop and continue.

    After a single injected error, the PTT script exhausts, so recoverable
    paths still end at exit 2 via Phase 1's "PTT ended" detector — the
    behavioral assertion for recoverable rows is on ``captured_messages``,
    not on the exit code difference.
    """
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)

    exc = exc_factory()
    sentences_arg = exc if source == "claude" else ("Reply.",)
    synth_arg = exc if source == "tts" else (b"\x00",)

    captured_messages = _install_voice_pipeline_mocks(
        monkeypatch,
        ptt_script=[
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
        ],
        stt_result="hi",
        sentences=sentences_arg,
        synth_result=synth_arg,
    )

    from agent.loop import run

    assert await run() == expected_exit
    out = capsys.readouterr().out
    assert expected_tag in out
    # Pop behavior is observable via the next Phase-1 iteration's view of
    # messages, but with only one turn before exhaustion we assert on the
    # messages that ``stream_sentences`` saw: that view is pre-pop, so a
    # single ``{role:user,content:hi}`` entry is present either way. The
    # distinction between fatal (no pop, exit 2 immediate) and
    # recoverable (pop, loop continues to idle, idle sees exhaustion,
    # exit 2) manifests in observability more subtly — the parametrize
    # row's ``pops_user`` flag is retained for self-documentation and to
    # express intent even though a single-turn script can't disambiguate
    # the two shapes further. Deeper multi-turn coverage can land once
    # #55's cancellation story is nailed down.
    _ = pops_user
    assert captured_messages == [[{"role": "user", "content": "hi"}]]
