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

import asyncio
from unittest.mock import MagicMock

import pytest

from agent import audio, ptt, stt, tts
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
    stt_result=None,
    stt_results=None,
    sentences=("ok.",),
    synth_result=(b"a",),
):
    """Install default fakes for ``agent.ptt.events``, ``agent.audio.capture``,
    ``agent.stt.transcribe``, ``agent.claude.stream_sentences``,
    ``agent.tts.synthesize``, ``agent.audio.playback``, and (#55 D5)
    no-op the startup audio probes so headless CI doesn't hit real
    PortAudio.

    Exactly one of ``stt_result`` (single value or exception, replayed on
    every turn) or ``stt_results`` (list indexed by call count, letting a
    test inject a different outcome per turn) must be set. The list form
    is what the F2 regression and F3 recoverable-multi-turn tests use to
    observe behavior across more than one turn.

    Returns the list of ``messages`` captured by the ``stream_sentences``
    stub so individual tests can assert on message-history state.
    """
    if (stt_result is None) == (stt_results is None):
        raise AssertionError("exactly one of stt_result / stt_results must be set")

    monkeypatch.setattr("agent.ptt.events", lambda: _scripted_async_gen(ptt_script))
    monkeypatch.setattr("agent.audio.capture", lambda *a, **kw: _scripted_async_gen([]))
    # #55 D5: default to probes-pass so the voice branch is reached.
    # Individual tests that exercise the fallback-to-text path
    # override these afterwards.
    monkeypatch.setattr("agent.audio.probe_input_device", lambda *a, **kw: None)
    monkeypatch.setattr("agent.audio.probe_output_device", lambda *a, **kw: None)

    stt_call_count = {"n": 0}

    async def _fake_transcribe(_chunks, _release):
        if stt_results is not None:
            idx = stt_call_count["n"]
            stt_call_count["n"] += 1
            result = stt_results[idx]
        else:
            result = stt_result
        if isinstance(result, BaseException):
            raise result
        return result

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


# --- T8: SIGINT / CancelledError at Phase 1 idle wait ------------------


async def test_voice_mode_cancelled_at_idle_exits_zero(monkeypatch, capsys):
    """CancelledError during Phase 1 idle wait → exit 0 + goodbye.

    On Python 3.11+, ``asyncio.run()`` delivers SIGINT by cancelling the
    main task, so the Phase 1 ``await ptt_queue.get()`` sees
    ``asyncio.CancelledError``, not ``KeyboardInterrupt``. The handler
    catches both together, so this one test (which injects CancelledError
    directly at the queue-get call site, mirroring the production
    propagation path) covers both signals.

    We stub the pump to a no-op and swap in a queue whose first ``get()``
    raises CancelledError, so the error arrives at the Phase 1 await
    rather than inside the pump (where it would be treated as a fatal
    source exhaustion and return 2 instead of 0).
    """
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)
    _install_voice_pipeline_mocks(
        monkeypatch,
        ptt_script=[],  # pump exhausts cleanly without emitting
        stt_result="never-reached",
    )

    class _CancellingQueue(asyncio.Queue):
        async def get(self):  # type: ignore[override]
            raise asyncio.CancelledError

    monkeypatch.setattr("agent.loop.asyncio.Queue", _CancellingQueue)

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


# --- T16+: Phase 5 error triage (Claude + TTS + generic) ---------------
#
# Split per CodeRabbit F3 into two tests so the pop-vs-no-pop behavioral
# distinction between fatal and recoverable rows is actually observed:
#
# * Fatal rows (single turn, exit 2 immediately): assert captured_messages
#   length == 1 and content shows the (unpopped) user turn.
# * Recoverable rows (two turns): assert captured_messages length == 2 and
#   turn 2's view shows ONLY the second user message — proving turn 1's
#   message was popped from history after the recoverable error.


@pytest.mark.parametrize(
    "source,exc_factory,expected_tag",
    [
        pytest.param(
            "claude",
            lambda: AuthenticationError("401", response=MagicMock(), body=None),
            "[auth]",
            id="claude-auth-fatal",
        ),
        pytest.param(
            "tts",
            lambda: tts.TTSAuthError("401 elevenlabs"),
            "[auth]",
            id="tts-auth-fatal",
        ),
    ],
)
async def test_voice_mode_phase5_fatal_exits_two_without_pop(
    monkeypatch, capsys, source, exc_factory, expected_tag
):
    """Fatal Phase-5 rows exit 2 immediately and do NOT pop the user turn (A1)."""
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

    assert await run() == 2
    out = capsys.readouterr().out
    assert expected_tag in out
    # Exactly one stream_sentences call; the user turn stayed on history
    # (fatal rows do not pop) and PTT death never got a chance to add
    # a second iteration — exit 2 is immediate from the fatal branch,
    # not from Phase 1's "PTT ended" detector.
    assert len(captured_messages) == 1
    assert captured_messages[0] == [{"role": "user", "content": "hi"}]


@pytest.mark.parametrize(
    "source,exc_factory,expected_tag",
    [
        pytest.param(
            "claude",
            lambda: RateLimitError("429", response=MagicMock(), body=None),
            "[rate]",
            id="claude-rate-recoverable",
        ),
        pytest.param(
            "claude",
            lambda: APIConnectionError(request=MagicMock()),
            "[net]",
            id="claude-net-recoverable",
        ),
        pytest.param(
            "tts",
            lambda: tts.TTSError("socket dropped"),
            "[tts]",
            id="tts-generic-recoverable",
        ),
        pytest.param(
            "tts",
            lambda: RuntimeError("surprise"),
            "[err]",
            id="unknown-exception-safety-net",
        ),
    ],
)
async def test_voice_mode_phase5_recoverable_pops_and_continues(
    monkeypatch, capsys, source, exc_factory, expected_tag
):
    """Recoverable Phase-5 rows pop the user turn and keep going.

    Two-turn script: turn 1 fails with the parametrized error (user turn
    expected to be popped); turn 2 completes cleanly. The behavioral
    assertion is that turn 2's ``stream_sentences`` view contains ONLY
    turn 2's user message — a leaked turn-1 entry would prove the pop
    didn't happen.
    """
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)

    # Turn 1 injects the error via the chosen seam; turn 2 streams a clean
    # reply. We pin ``sentences`` and ``synth_result`` to the *first*
    # turn's shape; the fakes are stateless w.r.t. call count so turn 2
    # re-uses the same stub behavior — for a recoverable row that means
    # Claude's turn 2 either yields the pinned sentence (TTS-side error)
    # or re-raises (Claude-side error). The latter would prevent us from
    # observing turn 2, so for Claude-side errors we flip ``sentences`` to
    # a plain string on second call via a small per-call wrapper.
    exc = exc_factory()
    sentences_calls = {"n": 0}
    synth_calls = {"n": 0}
    pinned_sentence = ("Reply.",)

    def _flaky_stream_sentences(messages, _system):
        captured_messages.append([dict(m) for m in messages])
        n = sentences_calls["n"]
        sentences_calls["n"] += 1
        if source == "claude" and n == 0:

            async def _raiser():
                raise exc
                yield  # pragma: no cover

            return _raiser()
        return _scripted_async_gen([("yield", s) for s in pinned_sentence])

    def _flaky_synthesize(text_chunks):
        n = synth_calls["n"]
        synth_calls["n"] += 1
        if source == "tts" and n == 0:

            async def _raiser():
                async for _ in text_chunks:
                    pass
                raise exc
                yield  # pragma: no cover

            return _raiser()

        async def _passthrough():
            async for _ in text_chunks:
                pass
            yield b"\x00"

        return _passthrough()

    captured_messages = _install_voice_pipeline_mocks(
        monkeypatch,
        ptt_script=[
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
        ],
        stt_results=["hi", "second"],
    )
    # Override the two seams the triage test cares about with per-call
    # fakes closed over the captured_messages list the installer returned.
    monkeypatch.setattr("agent.loop.stream_sentences", _flaky_stream_sentences)
    monkeypatch.setattr("agent.tts.synthesize", _flaky_synthesize)

    from agent.loop import run

    assert await run() == 2  # PTT exhausts after turn 2
    out = capsys.readouterr().out
    assert expected_tag in out
    # Two Claude calls: turn 1 saw [{user:hi}] (pre-pop), turn 2 saw
    # [{user:second}] — turn-1's "hi" was popped before turn 2.
    assert len(captured_messages) == 2
    assert captured_messages[0] == [{"role": "user", "content": "hi"}]
    assert captured_messages[1] == [{"role": "user", "content": "second"}]


# --- F2 regression: shared PTT source survives mid-turn watcher cancel --


async def test_voice_mode_recovers_from_stt_error_before_release(monkeypatch, capsys):
    """Recoverable STT error mid-turn must not brick the next turn.

    Pre-F2, cancelling the per-turn ``_watch_release`` task while it was
    suspended inside the shared ``ptt.events()`` generator ran the
    generator's ``finally`` (stopping the pynput Listener). The next
    Phase 1 iteration then saw ``StopAsyncIteration`` immediately and
    returned 2 — the first recoverable STT hiccup silently bricked voice
    mode. The pump pattern (``_ptt_pump`` → ``asyncio.Queue`` → consumers)
    isolates the generator so cancellation only cancels the consumer's
    ``queue.get()``, keeping the source alive for turn 2.

    Script: turn 1 PRESSED → STTError (recoverable) → turn 2 PRESSED →
    RELEASED → stt returns "second". Assertion: turn 2 reaches
    ``stream_sentences`` with ``[{role:user, content:second}]``, proving
    the pump survived turn 1's mid-turn watcher cancellation.
    """
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)
    captured_messages = _install_voice_pipeline_mocks(
        monkeypatch,
        ptt_script=[
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
        ],
        stt_results=[stt.STTError("boom"), "second"],
    )

    from agent.loop import run

    assert await run() == 2  # PTT exhausts after turn 2
    out = capsys.readouterr().out
    assert "[stt]" in out  # turn 1's recoverable error surfaced
    # Turn 2 made it to Claude — turn-1's STT error did not close the
    # shared PTT source. If F2 regresses, captured_messages is empty
    # (Phase 1 after the error sees ``None`` from the dead pump and
    # returns 2 before turn 2 can run).
    assert len(captured_messages) == 1
    assert captured_messages[0] == [{"role": "user", "content": "second"}]


# --- Bundle 7+ (#55): new error paths -----------------------------------


async def test_voice_mode_phase5_cancellederror_uncancels_and_continues(monkeypatch, capsys):
    """#55 D3: mid-stream Ctrl+C → task.uncancel() → [stopped] + continue.

    Two-turn script. Turn 1 injects asyncio.CancelledError into
    audio.playback; voice_loop should catch, uncancel (count 0 →
    swallow), print [stopped], pop the pending user turn, and keep
    going. Turn 2 completes cleanly. stream_sentences sees the second
    turn's user message only — proving pop happened before continue.
    """
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)

    play_calls = {"n": 0}

    async def _flaky_playback(chunks, *_a, **_kw):
        async for _ in chunks:
            pass
        n = play_calls["n"]
        play_calls["n"] += 1
        if n == 0:
            raise asyncio.CancelledError
        return

    captured_messages = _install_voice_pipeline_mocks(
        monkeypatch,
        ptt_script=[
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
        ],
        stt_results=["hi", "second"],
    )
    monkeypatch.setattr("agent.audio.playback", _flaky_playback)

    from agent.loop import run

    assert await run() == 2  # PTT exhausts after turn 2
    out = capsys.readouterr().out
    assert "[stopped]" in out
    assert len(captured_messages) == 2
    assert captured_messages[0] == [{"role": "user", "content": "hi"}]
    assert captured_messages[1] == [{"role": "user", "content": "second"}]


async def test_voice_mode_phase5_cancellederror_external_reraises(monkeypatch, capsys):
    """#55 D3: external cancellation (uncancel() > 0) must re-raise."""
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)

    async def _playback_double_cancels(chunks, *_a, **_kw):
        async for _ in chunks:
            pass
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        task.cancel()
        raise asyncio.CancelledError

    _install_voice_pipeline_mocks(
        monkeypatch,
        ptt_script=[
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
        ],
        stt_result="hi",
    )
    monkeypatch.setattr("agent.audio.playback", _playback_double_cancels)

    from agent.loop import run

    with pytest.raises(asyncio.CancelledError):
        await run()
    out = capsys.readouterr().out
    assert "[stopped]" not in out


async def test_voice_mode_phase5_network_error_recovers(monkeypatch, capsys):
    """#55 D4: NetworkError mid-Phase-5 → [net] + pop + continue."""
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)

    from agent.claude import NetworkError

    captured_messages = _install_voice_pipeline_mocks(
        monkeypatch,
        ptt_script=[
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
        ],
        stt_result="hi",
        sentences=NetworkError("peer closed mid-SSE"),
    )

    from agent.loop import run

    assert await run() == 2
    out = capsys.readouterr().out
    assert "[net] Lost connection mid-response" in out
    assert captured_messages == [[{"role": "user", "content": "hi"}]]


async def test_voice_mode_phase5_playback_error_prints_text_reply(monkeypatch, capsys):
    """#55 D1 + R5: playback dies mid-stream → [audio] + text reply + keep assistant turn."""
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)

    async def _playback_dies(chunks, *_a, **_kw):
        async for _ in chunks:
            pass
        raise audio.AudioPlaybackError("output stream stalled (10 consecutive underflows)")

    _install_voice_pipeline_mocks(
        monkeypatch,
        ptt_script=[
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
        ],
        stt_results=["hi", "next turn"],
        sentences=("Hello.", " How can I help?"),
    )
    monkeypatch.setattr("agent.audio.playback", _playback_dies)

    captured_messages: list = []

    def _recording_stream_sentences(messages, _system):
        captured_messages.append([dict(m) for m in messages])
        return _scripted_async_gen([("yield", "Hello."), ("yield", " How can I help?")])

    monkeypatch.setattr("agent.loop.stream_sentences", _recording_stream_sentences)

    from agent.loop import run

    assert await run() == 2
    out = capsys.readouterr().out
    assert "[audio]" in out
    assert "friday > Hello. How can I help?" in out
    assert len(captured_messages) == 2
    assert captured_messages[1] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Hello. How can I help?"},
        {"role": "user", "content": "next turn"},
    ]


async def test_voice_mode_phase5_playback_error_empty_collect(monkeypatch, capsys):
    """#55 R6: playback dies BEFORE any sentence emitted → pop user, no 'friday >' line."""
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)

    async def _playback_dies_before_any_chunk(chunks, *_a, **_kw):
        raise audio.AudioPlaybackError("device unplugged before first frame")

    captured_messages = _install_voice_pipeline_mocks(
        monkeypatch,
        ptt_script=[
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
        ],
        stt_results=["hi", "retry"],
    )
    monkeypatch.setattr("agent.audio.playback", _playback_dies_before_any_chunk)

    from agent.loop import run

    assert await run() == 2
    out = capsys.readouterr().out
    assert "[audio]" in out
    assert "friday >" not in out
    assert len(captured_messages) == 2
    assert captured_messages[1] == [{"role": "user", "content": "retry"}]


async def test_voice_mode_phase5_tts_error_apologizes_textually(monkeypatch, capsys):
    """#55 D2: TTSError → [tts] + [friday] text apology + pop + continue."""
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)

    _install_voice_pipeline_mocks(
        monkeypatch,
        ptt_script=[
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
        ],
        stt_result="hi",
        synth_result=tts.TTSError("websocket dropped"),
    )

    from agent.loop import run

    assert await run() == 2
    out = capsys.readouterr().out
    assert "[tts]" in out
    assert "Sorry, I lost my voice. Try again." in out


async def test_voice_mode_anthropic_auth_names_env_var(monkeypatch, capsys):
    """#55 D6: mid-session AuthenticationError message names the env var."""
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)

    _install_voice_pipeline_mocks(
        monkeypatch,
        ptt_script=[
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
        ],
        stt_result="hi",
        sentences=AuthenticationError("401", response=MagicMock(), body=None),
    )

    from agent.loop import run

    assert await run() == 2
    out = capsys.readouterr().out
    assert "ANTHROPIC_API_KEY" in out
    assert "[auth]" in out


async def test_text_mode_cancellederror_mid_stream_exits_zero(monkeypatch, capsys):
    """#55 D3: text_loop mid-stream CancelledError → [goodbye] + exit 0."""
    _set_text_argv(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)
    monkeypatch.setattr("builtins.input", MagicMock(return_value="hi"))

    async def _raising_stream(_messages, _system):
        yield "Hello "
        raise asyncio.CancelledError

    monkeypatch.setattr("agent.loop.stream_tokens", _raising_stream)

    from agent.loop import run

    assert await run() == 0
    out = capsys.readouterr().out
    assert "goodbye" in out


async def test_text_mode_keyboardinterrupt_mid_stream_exits_zero(monkeypatch, capsys):
    """#55 D3 (R4 defensive): text_loop mid-stream KeyboardInterrupt → exit 0."""
    _set_text_argv(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)
    monkeypatch.setattr("builtins.input", MagicMock(return_value="hi"))

    async def _raising_stream(_messages, _system):
        yield "Hello "
        raise KeyboardInterrupt

    monkeypatch.setattr("agent.loop.stream_tokens", _raising_stream)

    from agent.loop import run

    assert await run() == 0
    out = capsys.readouterr().out
    assert "goodbye" in out


async def test_run_falls_back_to_text_on_mic_unavailable(monkeypatch, capsys):
    """#55 D5: AudioDeviceUnavailable from input probe → text_loop dispatched."""
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)

    def _bad_input_probe(*_a, **_kw):
        raise audio.AudioDeviceUnavailable("no usable input device: no default")

    monkeypatch.setattr("agent.audio.probe_input_device", _bad_input_probe)
    monkeypatch.setattr("agent.audio.probe_output_device", lambda *a, **kw: None)
    monkeypatch.setattr("builtins.input", MagicMock(side_effect=KeyboardInterrupt))

    from agent.loop import run

    assert await run() == 0
    out = capsys.readouterr().out
    assert "[audio]" in out
    assert "falling back to text mode" in out


async def test_run_falls_back_to_text_on_speaker_unavailable(monkeypatch, capsys):
    """#55 D5: AudioDeviceUnavailable from output probe → text_loop dispatched."""
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)

    def _bad_output_probe(*_a, **_kw):
        raise audio.AudioDeviceUnavailable("no usable output device: no default")

    monkeypatch.setattr("agent.audio.probe_input_device", lambda *a, **kw: None)
    monkeypatch.setattr("agent.audio.probe_output_device", _bad_output_probe)
    monkeypatch.setattr("builtins.input", MagicMock(side_effect=KeyboardInterrupt))

    from agent.loop import run

    assert await run() == 0
    out = capsys.readouterr().out
    assert "[audio]" in out
    assert "falling back to text mode" in out
