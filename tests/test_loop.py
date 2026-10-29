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
import contextlib
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
    barge_in=None,
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

    # #72 commit 4: stub detect_barge_in so tests don't attempt a real
    # Flux connection. Default behaviour: wait on the shared event
    # forever (caller cancels on teardown — matches production shape).
    # Tests can override via ``barge_in`` which runs on top of the
    # default (e.g. firing the event after a short delay).
    async def _fake_detect_barge_in(mic_chunks, barge_in_event, **_kw):
        # Drain the mic iter so audio.capture()'s generator sees the
        # same lifecycle as production. Running concurrently with the
        # waiting-on-event task is what the real detect_barge_in does.
        async def _drain() -> None:
            async for _ in mic_chunks:
                pass

        drain_task = asyncio.create_task(_drain(), name="fake-detect-mic-drain")
        if barge_in is not None:
            await barge_in(barge_in_event)
        try:
            await asyncio.Event().wait()  # block until cancelled
        finally:
            if not drain_task.done():
                drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await drain_task

    monkeypatch.setattr("agent.stt.detect_barge_in", _fake_detect_barge_in)

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
    """#55 D3 / #72 commit 4: external cancellation of the voice_loop
    task (uncancel() > 0) must re-raise.

    Mechanism change in #72 commit 4: pre-restructure, Phase 5 ran
    ``audio.playback`` as an inline await, so ``task.cancel()`` inside
    the playback body cancelled the voice_loop task itself. Post-
    restructure, playback runs under its own ``asyncio.Task`` as a
    sibling to the barge-in detector, so inner ``task.cancel()`` only
    cancels the playback task — exactly the "per-turn interrupt" path
    the production code handles via uncancel() returning 0.

    This test now cancels the OUTER task directly, which is the real
    "external cancellation" scenario (TaskGroup child cancel, direct
    task.cancel from a supervisor). The double-cancel on the outer
    task pushes uncancel() to return 1, re-raising from Phase 5.
    """
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)

    outer_ref: list[asyncio.Task | None] = [None]

    async def _playback_cancels_outer(chunks, *_a, **_kw):
        async for _ in chunks:
            pass
        outer = outer_ref[0]
        assert outer is not None, "outer task ref not populated"
        outer.cancel()
        outer.cancel()
        # Yield once so the double-cancel takes effect on the outer
        # task's next await (which is the asyncio.wait inside
        # voice_loop's Phase 5).
        await asyncio.sleep(0)
        raise asyncio.CancelledError

    _install_voice_pipeline_mocks(
        monkeypatch,
        ptt_script=[
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
        ],
        stt_result="hi",
    )
    monkeypatch.setattr("agent.audio.playback", _playback_cancels_outer)

    from agent.loop import run

    outer = asyncio.create_task(run())
    outer_ref[0] = outer
    with pytest.raises(asyncio.CancelledError):
        await outer
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


# ---------------------------------------------------------------------------
# #72 commit 3: state machine refactor — observer stubs + recovery helper
# ---------------------------------------------------------------------------

from agent.events import BargeInEvent  # noqa: E402
from agent.loop import (  # noqa: E402
    _recover_to_listening_from_phase5,
    _TranscriptStore,
    _TurnMetrics,
)
from agent.turn_state import TurnState, TurnStateMachine  # noqa: E402


def _walk_sm(sm: TurnStateMachine, *steps: TurnState) -> None:
    """Walk the machine through the given states, each as a legal
    transition with trigger='force'. Setup helper for observer tests."""
    for s in steps:
        assert sm.transition(s, trigger="loop"), (
            f"walk failed at {s.name}; current={sm.current().name}"
        )


# --- _TranscriptStore ------------------------------------------------------


def test_transcript_store_initial_state_is_blank():
    store = _TranscriptStore()
    assert store.last_outcome is None
    assert store.last_elapsed_text is None


def test_transcript_store_marks_spoken_on_clean_speaking_to_listening():
    """SPEAKING -> LISTENING is the clean-turn-end edge and must mark
    the assistant turn as ``spoken``."""
    sm = TurnStateMachine()
    store = _TranscriptStore()
    sm.on_change(store.observe)
    _walk_sm(sm, TurnState.LISTENING, TurnState.THINKING, TurnState.SPEAKING)
    assert store.last_outcome is None  # still unset — no SPEAKING-origin edge yet

    sm.transition(TurnState.LISTENING, trigger="loop")
    assert store.last_outcome == "spoken"


def test_transcript_store_marks_interrupted_on_barge_in():
    """SPEAKING -> BARGE_IN is the barge-in edge (commit 4 fires it)
    and must mark the assistant turn as ``interrupted``. Carry 1 from
    the commit-2 review: this must be encoded in commit 3, not left
    as a stub."""
    sm = TurnStateMachine()
    store = _TranscriptStore()
    sm.on_change(store.observe)
    _walk_sm(sm, TurnState.LISTENING, TurnState.THINKING, TurnState.SPEAKING)

    sm.transition(TurnState.BARGE_IN, trigger="flux")
    assert store.last_outcome == "interrupted"


def test_transcript_store_ignores_non_speaking_origin_transitions():
    sm = TurnStateMachine()
    store = _TranscriptStore()
    sm.on_change(store.observe)
    _walk_sm(sm, TurnState.LISTENING, TurnState.THINKING)
    assert store.last_outcome is None
    sm.transition(TurnState.LISTENING, trigger="loop")  # THINKING -> LISTENING
    assert store.last_outcome is None


# --- _TurnMetrics ----------------------------------------------------------


def test_turn_metrics_counts_each_transition_exactly_once():
    sm = TurnStateMachine()
    metrics = _TurnMetrics()
    sm.on_change(metrics.observe)
    _walk_sm(
        sm,
        TurnState.LISTENING,
        TurnState.THINKING,
        TurnState.SPEAKING,
        TurnState.LISTENING,
    )
    assert metrics.transitions_seen == 4
    assert metrics.by_edge[(TurnState.IDLE, TurnState.LISTENING)] == 1
    assert metrics.by_edge[(TurnState.LISTENING, TurnState.THINKING)] == 1
    assert metrics.by_edge[(TurnState.THINKING, TurnState.SPEAKING)] == 1
    assert metrics.by_edge[(TurnState.SPEAKING, TurnState.LISTENING)] == 1


def test_turn_metrics_illegal_transition_does_not_increment():
    sm = TurnStateMachine()
    metrics = _TurnMetrics()
    sm.on_change(metrics.observe)
    _walk_sm(sm, TurnState.LISTENING)
    assert metrics.transitions_seen == 1
    # SPEAKING from LISTENING is illegal — observer should not fire.
    accepted = sm.transition(TurnState.SPEAKING, trigger="loop")
    assert accepted is False
    assert metrics.transitions_seen == 1


# --- _recover_to_listening_from_phase5 -------------------------------------


def test_recover_to_listening_from_thinking():
    sm = TurnStateMachine()
    _walk_sm(sm, TurnState.LISTENING, TurnState.THINKING)
    _recover_to_listening_from_phase5(sm, trigger="manual")
    assert sm.current() == TurnState.LISTENING


def test_recover_to_listening_from_speaking():
    sm = TurnStateMachine()
    _walk_sm(sm, TurnState.LISTENING, TurnState.THINKING, TurnState.SPEAKING)
    _recover_to_listening_from_phase5(sm, trigger="manual")
    assert sm.current() == TurnState.LISTENING


def test_recover_from_listening_is_noop():
    """Caller shouldn't need to track whether Phase 5 was entered —
    recovery helper is safe to call from any state and only acts on
    THINKING / SPEAKING."""
    sm = TurnStateMachine()
    _walk_sm(sm, TurnState.LISTENING)
    _recover_to_listening_from_phase5(sm, trigger="manual")
    assert sm.current() == TurnState.LISTENING  # unchanged


def test_recover_from_idle_is_noop():
    sm = TurnStateMachine()
    _recover_to_listening_from_phase5(sm, trigger="manual")
    assert sm.current() == TurnState.IDLE  # unchanged — no illegal attempt


def test_recover_to_listening_from_barge_in():
    """CR #86 F4: mid-aclose or mid-cancel after SPEAKING -> BARGE_IN
    fired must still recover to LISTENING. Pre-F4 the helper only
    handled THINKING / SPEAKING, so cancellation during
    ``barge_in_mic.aclose()`` would leave the machine stuck in
    BARGE_IN and the next PRESSED would attempt an illegal
    BARGE_IN -> THINKING."""
    sm = TurnStateMachine()
    _walk_sm(sm, TurnState.LISTENING, TurnState.THINKING, TurnState.SPEAKING, TurnState.BARGE_IN)
    _recover_to_listening_from_phase5(sm, trigger="manual")
    assert sm.current() == TurnState.LISTENING


# ---------------------------------------------------------------------------
# Lift agent/loop.py coverage over the 90% bar — small text_loop paths that
# existed pre-#72 but were uncovered.
# ---------------------------------------------------------------------------


async def test_text_mode_empty_input_skips_claude(monkeypatch, capsys):
    """Empty input (just Enter) hits the ``if not user: continue`` short
    circuit and never calls stream_tokens; next input then exits."""
    _set_text_argv(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)
    inputs = iter(["   ", "", KeyboardInterrupt])

    def _fake_input(_prompt: str = "") -> str:
        v = next(inputs)
        if isinstance(v, type) and issubclass(v, BaseException):
            raise v
        return v

    monkeypatch.setattr("builtins.input", _fake_input)
    stream_called = []

    async def _stream_tokens(_messages, _system):
        stream_called.append(1)
        yield "unreached"

    monkeypatch.setattr("agent.loop.stream_tokens", _stream_tokens)

    from agent.loop import run

    assert await run() == 0
    assert not stream_called, "stream_tokens must not be called on empty input"


async def test_text_mode_rate_limit_pops_and_continues(monkeypatch, capsys):
    """RateLimitError mid-stream prints [rate] and the next input exits."""
    from anthropic import RateLimitError as _RL

    _set_text_argv(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)
    inputs = iter(["hello", KeyboardInterrupt])

    def _fake_input(_prompt: str = "") -> str:
        v = next(inputs)
        if isinstance(v, type) and issubclass(v, BaseException):
            raise v
        return v

    monkeypatch.setattr("builtins.input", _fake_input)

    async def _raising_stream(_messages, _system):
        if False:  # pragma: no cover
            yield ""
        raise _RL("rate", response=MagicMock(status_code=429), body=None)

    monkeypatch.setattr("agent.loop.stream_tokens", _raising_stream)

    from agent.loop import run

    assert await run() == 0
    out = capsys.readouterr().out
    assert "[rate]" in out


async def test_text_mode_network_error_pops_and_continues(monkeypatch, capsys):
    """NetworkError (seam-wrapped mid-stream drop) prints [net] and the
    next input exits."""
    from agent.claude import NetworkError as _NE

    _set_text_argv(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)
    inputs = iter(["hello", KeyboardInterrupt])

    def _fake_input(_prompt: str = "") -> str:
        v = next(inputs)
        if isinstance(v, type) and issubclass(v, BaseException):
            raise v
        return v

    monkeypatch.setattr("builtins.input", _fake_input)

    async def _raising_stream(_messages, _system):
        if False:  # pragma: no cover
            yield ""
        raise _NE("transport closed")

    monkeypatch.setattr("agent.loop.stream_tokens", _raising_stream)

    from agent.loop import run

    assert await run() == 0
    out = capsys.readouterr().out
    assert "[net]" in out


async def test_text_mode_authentication_error_exits_two(monkeypatch, capsys):
    """Mid-stream AuthenticationError exits 2 and names the env var."""
    from anthropic import AuthenticationError as _AE

    _set_text_argv(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)
    monkeypatch.setattr("builtins.input", MagicMock(return_value="hello"))

    async def _raising_stream(_messages, _system):
        if False:  # pragma: no cover
            yield ""
        raise _AE("auth", response=MagicMock(status_code=401), body=None)

    monkeypatch.setattr("agent.loop.stream_tokens", _raising_stream)

    from agent.loop import run

    assert await run() == 2
    out = capsys.readouterr().out
    assert "[auth]" in out
    assert "ANTHROPIC_API_KEY" in out


async def test_text_mode_generic_exception_pops_and_continues(monkeypatch, capsys):
    """A random Exception mid-stream hits the BLE001 safety net."""
    _set_text_argv(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)
    inputs = iter(["hello", KeyboardInterrupt])

    def _fake_input(_prompt: str = "") -> str:
        v = next(inputs)
        if isinstance(v, type) and issubclass(v, BaseException):
            raise v
        return v

    monkeypatch.setattr("builtins.input", _fake_input)

    async def _raising_stream(_messages, _system):
        if False:  # pragma: no cover
            yield ""
        raise RuntimeError("something odd")

    monkeypatch.setattr("agent.loop.stream_tokens", _raising_stream)

    from agent.loop import run

    assert await run() == 0
    out = capsys.readouterr().out
    assert "[err]" in out
    assert "RuntimeError" in out


# ---------------------------------------------------------------------------
# #72 commit 4: SPEAKING -> BARGE_IN -> LISTENING on confirmed barge-in
# ---------------------------------------------------------------------------


async def test_voice_mode_barge_in_fires_speaking_to_barge_in(monkeypatch, capsys):
    """A confirmed barge-in (shared asyncio.Event set during SPEAKING)
    fires SPEAKING -> BARGE_IN with a BargeInEvent payload, then
    BARGE_IN -> LISTENING after the (stubbed) TTS cancel completes."""
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)

    seen: list[tuple[TurnState, TurnState, str, object]] = []

    class _TappedMachine(TurnStateMachine):
        def __init__(self) -> None:
            super().__init__()
            self.on_change(lambda f, t, trig, p: seen.append((f, t, trig, p)))

    monkeypatch.setattr("agent.loop.TurnStateMachine", _TappedMachine)

    async def _fire_barge_in(event: asyncio.Event) -> None:
        # Give playback a tick to start, then fire the shared event.
        await asyncio.sleep(0.001)
        event.set()

    # Pre-roll the ptt script so the loop progresses past Phase 1 and
    # into Phase 5 before we fire the barge-in signal. After the
    # barge-in-driven continue, exhaust the ptt stream so run()
    # returns 2.
    _install_voice_pipeline_mocks(
        monkeypatch,
        ptt_script=[
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
        ],
        stt_result="hi",
        sentences=("Hello.",),
        barge_in=_fire_barge_in,
    )

    # Make playback long enough that the barge-in event fires BEFORE
    # playback completes naturally — otherwise the race could resolve
    # as a clean turn end.
    async def _slow_playback(chunks, *_a, **_kw):
        async for _ in chunks:
            pass
        await asyncio.sleep(0.1)

    monkeypatch.setattr("agent.audio.playback", _slow_playback)

    from agent.loop import run

    assert await run() == 2  # ptt exhausts after the barged-in turn
    out = capsys.readouterr().out
    assert "[barged in]" in out

    # The sequence must contain SPEAKING -> BARGE_IN with trigger="flux"
    # and then BARGE_IN -> LISTENING with trigger="loop".
    edges = [(f, t, trig) for (f, t, trig, _p) in seen]
    assert (TurnState.SPEAKING, TurnState.BARGE_IN, "flux") in edges
    assert (TurnState.BARGE_IN, TurnState.LISTENING, "loop") in edges

    # Payload on SPEAKING -> BARGE_IN must be a BargeInEvent with
    # source="flux", confidence None, and a monotonic timestamp.
    (bargein_payload,) = [
        p for (f, t, _trig, p) in seen if f == TurnState.SPEAKING and t == TurnState.BARGE_IN
    ]
    assert isinstance(bargein_payload, BargeInEvent)
    assert bargein_payload.source == "flux"
    assert bargein_payload.confidence is None
    assert isinstance(bargein_payload.at, float)


async def test_voice_mode_barge_in_discards_user_turn(monkeypatch, capsys):
    """BARGE_IN pops the pending user turn from messages — the user
    interrupted, so the half-spoken reply doesn't commit to history
    for Claude's next turn. Matches the SIGINT-stopped pattern."""
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)

    captured_messages_ref = {"val": None}

    async def _fire_barge_in(event: asyncio.Event) -> None:
        await asyncio.sleep(0.001)
        event.set()

    captured_messages_ref["val"] = _install_voice_pipeline_mocks(
        monkeypatch,
        ptt_script=[
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
        ],
        stt_results=["first turn", "second turn"],
        sentences=("ok.",),
        barge_in=_fire_barge_in,
    )

    async def _slow_playback(chunks, *_a, **_kw):
        async for _ in chunks:
            pass
        await asyncio.sleep(0.1)

    monkeypatch.setattr("agent.audio.playback", _slow_playback)

    from agent.loop import run

    assert await run() == 2
    out = capsys.readouterr().out
    assert "[barged in]" in out

    # First turn: user said "first turn", barged in, pop.
    # Second turn: user said "second turn", claude's _fake_stream_sentences
    # captures messages snapshot. The second turn's captured messages
    # must NOT contain "first turn" as a prior user turn (barge-in
    # discarded it).
    captured = captured_messages_ref["val"]
    assert len(captured) == 2
    assert captured[0] == [{"role": "user", "content": "first turn"}]
    assert captured[1] == [{"role": "user", "content": "second turn"}]


async def test_voice_mode_barge_in_marks_transcript_store_interrupted(monkeypatch, capsys):
    """_TranscriptStore.last_outcome must flip to 'interrupted' on the
    SPEAKING -> BARGE_IN edge; last_elapsed_text records what the
    user heard before the cut."""
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)

    captured_store: dict[str, _TranscriptStore] = {}

    original_store_cls = _TranscriptStore

    class _CapturingStore(original_store_cls):
        def __init__(self) -> None:
            super().__init__()
            captured_store["val"] = self

    monkeypatch.setattr("agent.loop._TranscriptStore", _CapturingStore)

    async def _fire_barge_in(event: asyncio.Event) -> None:
        await asyncio.sleep(0.001)
        event.set()

    _install_voice_pipeline_mocks(
        monkeypatch,
        ptt_script=[
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
        ],
        stt_result="hi",
        sentences=("Hello there.",),
        barge_in=_fire_barge_in,
    )

    async def _slow_playback(chunks, *_a, **_kw):
        async for _ in chunks:
            pass
        await asyncio.sleep(0.1)

    monkeypatch.setattr("agent.audio.playback", _slow_playback)

    from agent.loop import run

    assert await run() == 2
    store = captured_store["val"]
    assert store.last_outcome == "interrupted"
    assert store.last_elapsed_text == "Hello there."


# ---------------------------------------------------------------------------
# #87: defer THINKING -> SPEAKING until first audio chunk
# ---------------------------------------------------------------------------


async def test_voice_mode_empty_synth_stream_takes_thinking_to_listening_directly(
    monkeypatch, capsys
):
    """#87: when tts.synthesize yields zero chunks (LLM produced
    nothing speakable), Phase 5 must transition THINKING ->
    LISTENING directly without entering SPEAKING, without spawning
    the barge-in detector, and the pending user turn is popped
    with a [no reply] breadcrumb.

    Role-alternation parity: the next turn's captured messages
    must NOT contain the discarded user turn (same shape as
    test_voice_mode_barge_in_discards_user_turn).
    """
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)

    seen: list[tuple[TurnState, TurnState, str, object]] = []

    class _TappedMachine(TurnStateMachine):
        def __init__(self) -> None:
            super().__init__()
            self.on_change(lambda f, t, trig, p: seen.append((f, t, trig, p)))

    monkeypatch.setattr("agent.loop.TurnStateMachine", _TappedMachine)

    captured = _install_voice_pipeline_mocks(
        monkeypatch,
        ptt_script=[
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
        ],
        stt_results=["first turn", "second turn"],
        sentences=("ok.",),
        synth_result=(),  # empty tuple drives the zero-chunk path
    )

    from agent.loop import run

    assert await run() == 2  # ptt exhausts after the second turn
    out = capsys.readouterr().out
    assert "[no reply]" in out

    edges = [(f, t, trig) for (f, t, trig, _p) in seen]
    assert (TurnState.LISTENING, TurnState.THINKING, "loop") in edges
    assert (TurnState.THINKING, TurnState.LISTENING, "loop") in edges
    assert not any(f == TurnState.THINKING and t == TurnState.SPEAKING for (f, t, _trig) in edges)
    assert not any(f == TurnState.SPEAKING for (f, t, _trig) in edges)

    # Role-alternation: each turn's stream_sentences snapshot sees
    # only its own user message. The prior empty-stream turn's
    # discarded user is popped and never leaks into the next turn.
    assert len(captured) == 2
    assert captured[0] == [{"role": "user", "content": "first turn"}]
    assert captured[1] == [{"role": "user", "content": "second turn"}]


async def test_voice_mode_detect_barge_in_not_spawned_on_empty_stream(monkeypatch):
    """#87: on the empty-stream path, ``stt.detect_barge_in`` is
    never invoked — Phase 5 takes THINKING -> LISTENING directly
    before reaching the SPEAKING-entry barge-in detector spawn."""
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)

    machine_ref: list[TurnStateMachine] = []

    class _CapturingMachine(TurnStateMachine):
        def __init__(self) -> None:
            super().__init__()
            machine_ref.append(self)

    monkeypatch.setattr("agent.loop.TurnStateMachine", _CapturingMachine)

    records: list[tuple[bool, TurnState]] = []

    async def _recording_detect(mic_chunks, _barge_in_event, **_kw):
        # Record (invoked, state-at-invocation) BEFORE draining.
        state = machine_ref[0].current() if machine_ref else None
        records.append((True, state))

        # Mirror _fake_detect_barge_in production shape so the mic
        # generator sees the same lifecycle under cleanup.
        async def _drain() -> None:
            async for _ in mic_chunks:
                pass

        drain_task = asyncio.create_task(_drain(), name="rec-detect-mic-drain")
        try:
            await asyncio.Event().wait()  # block until cancelled
        finally:
            if not drain_task.done():
                drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await drain_task

    _install_voice_pipeline_mocks(
        monkeypatch,
        ptt_script=[
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
        ],
        stt_result="hi",
        sentences=("ok.",),
        synth_result=(),
    )
    # Override after _install_voice_pipeline_mocks (which installs
    # its own _fake_detect_barge_in last).
    monkeypatch.setattr("agent.stt.detect_barge_in", _recording_detect)

    from agent.loop import run

    assert await run() == 2
    assert records == []


async def test_voice_mode_detect_barge_in_spawned_after_speaking_on_happy_path(monkeypatch):
    """#87: on the happy path (non-empty synth), ``stt.detect_barge_in``
    is invoked exactly once, AFTER the THINKING -> SPEAKING
    transition has fired on the state machine."""
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)

    machine_ref: list[TurnStateMachine] = []

    class _CapturingMachine(TurnStateMachine):
        def __init__(self) -> None:
            super().__init__()
            machine_ref.append(self)

    monkeypatch.setattr("agent.loop.TurnStateMachine", _CapturingMachine)

    records: list[tuple[bool, TurnState]] = []

    async def _recording_detect(mic_chunks, _barge_in_event, **_kw):
        state = machine_ref[0].current() if machine_ref else None
        records.append((True, state))

        async def _drain() -> None:
            async for _ in mic_chunks:
                pass

        drain_task = asyncio.create_task(_drain(), name="rec-detect-mic-drain")
        try:
            await asyncio.Event().wait()
        finally:
            if not drain_task.done():
                drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await drain_task

    _install_voice_pipeline_mocks(
        monkeypatch,
        ptt_script=[
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
        ],
        stt_result="hi",
        sentences=("ok.",),
        synth_result=(b"a",),
    )
    monkeypatch.setattr("agent.stt.detect_barge_in", _recording_detect)

    from agent.loop import run

    assert await run() == 2
    assert len(records) == 1
    (_invoked, state_at_invocation) = records[0]
    assert state_at_invocation == TurnState.SPEAKING


async def test_voice_mode_pre_roll_cancellation_cleans_up_playback_task(monkeypatch):
    """CR #92 F2: an external CancelledError during the pre-race
    (before first chunk) must cancel + await ``playback_task`` so it
    does not leak across turns. Without the inner-finally pre-roll
    cleanup, the orphaned task keeps consuming its synthesis stream
    and can play audio during a later turn.

    Deterministic: the mock playback sets ``playback_started`` once
    it enters, so the test cancels main_task only after we know the
    pre-race is live.
    """
    _set_voice_argv(monkeypatch)
    _set_all_keys(monkeypatch)
    monkeypatch.setattr("agent.loop.load_dotenv", lambda: None)

    playback_started = asyncio.Event()
    playback_tasks: list[asyncio.Task] = []

    async def _hangs_in_pre_roll(_chunks, *_a, **_kw):
        # Register this task + signal pre-roll has begun, then block
        # forever. first_chunk_event stays unset because we never
        # consume a chunk from _chunks.
        playback_tasks.append(asyncio.current_task())
        playback_started.set()
        await asyncio.Event().wait()  # cancelled by voice_loop's cleanup

    _install_voice_pipeline_mocks(
        monkeypatch,
        ptt_script=[
            ("yield", ptt.PTTEvent.PRESSED),
            ("yield", ptt.PTTEvent.RELEASED),
        ],
        stt_result="hi",
        sentences=("ok.",),
        synth_result=(b"a",),  # synth has data, but _hangs_in_pre_roll never reads it
    )
    monkeypatch.setattr("agent.audio.playback", _hangs_in_pre_roll)

    from agent.loop import run

    main_task = asyncio.create_task(run())
    await playback_started.wait()  # pre-race is live
    main_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await main_task

    # Exactly one playback task was spawned; the pre-roll cleanup
    # must have driven it to a terminal state (cancelled). Without
    # the F2 fix, it would still be pending.
    assert len(playback_tasks) == 1
    assert playback_tasks[0].done()
