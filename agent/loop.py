"""Tier 3 conversation loops — text REPL + voice pipeline over Claude.

Two async coroutines drive user interaction. ``text_loop()`` preserves
the Tier-2 keyboard REPL (opened by ``python -m agent --text``);
``voice_loop()`` is the Tier-3 default push-to-talk pipeline composing
``agent.audio``, ``agent.ptt``, ``agent.stt``, ``agent.claude``, and
``agent.tts``. ``run()`` is the single entry point ``agent/__main__.py``
calls — it parses ``sys.argv`` with argparse, does shared startup
(``load_dotenv`` + per-branch API-key check), and dispatches to one
loop or the other, returning its int exit code.

The three streaming SDKs (anthropic, deepgram, elevenlabs) each sit
behind a dedicated seam module per ADR-0003; this file consumes those
public surfaces unchanged. Both loops share ``agent/claude.py`` as the
single LLM seam per ADR-0002: ``stream_tokens`` for text REPL
character-level streaming, ``stream_sentences`` for voice-path
sentence-chunking into TTS.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
from collections.abc import AsyncIterator

from dotenv import load_dotenv

from agent import audio, ptt, stt, tts
from agent.claude import (
    APIConnectionError,
    AuthenticationError,
    RateLimitError,
    stream_sentences,
    stream_tokens,
)
from agent.system_prompt import SYSTEM_PROMPT

# --- Banners + stock messages -------------------------------------------
_BANNER: str = "FRIDAY — Tier 1 baseline. Type to talk. Ctrl+C to exit."
_VOICE_BANNER: str = "FRIDAY — voice mode. Hold Right Alt to talk. Ctrl+C to exit."
_MISSING_KEY: str = (
    "ANTHROPIC_API_KEY isn't set; see .env.example. Copy it to .env and add your key."
)
_GOODBYE: str = "\n[goodbye]"

# Env vars checked up front for the voice branch, in the order they're
# listed back to the user on a missing-key error. Order follows
# ``.env.example`` (alphabetical Anthropic → Deepgram → ElevenLabs).
_VOICE_REQUIRED_KEYS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "DEEPGRAM_API_KEY",
    "ELEVENLABS_API_KEY",
)


def _voice_missing_keys_message(missing: list[str]) -> str:
    """Compose the voice-branch missing-keys message per A4 of the plan.

    One ``print()`` body: each missing var's own
    ``<VAR> isn't set; see .env.example.`` line followed by a single
    ``Copy .env.example to .env and add your keys.`` footer. Mirrors
    the shape of ``_MISSING_KEY`` so text-mode and voice-mode UX read
    consistently.
    """
    lines = [f"{var} isn't set; see .env.example." for var in missing]
    lines.append("Copy .env.example to .env and add your keys.")
    return "\n".join(lines)


async def _tee(src: AsyncIterator[str], collect: list[str]) -> AsyncIterator[str]:
    """Pass sentence chunks through to TTS while collecting them.

    Lifted to module scope (not a closure) so a reused ``collect``
    list belongs to each turn's local scope in ``voice_loop`` without
    tripping Ruff's B023 or confusing the GC.
    """
    async for chunk in src:
        collect.append(chunk)
        yield chunk


async def _watch_release(
    ptt_iter: AsyncIterator[ptt.PTTEvent],
    ptt_release: asyncio.Event,
) -> None:
    """Watch the held ``ptt.events()`` iterator for the next RELEASED.

    Sets ``ptt_release`` as soon as RELEASED arrives so
    ``stt.transcribe`` can finalize via its ForceEndTurn path. On any
    exit path (RELEASED seen, iterator exhausted, exception
    propagated) the Event is set in the ``finally`` block so a
    pending transcribe never hangs forever on PTT death — the next
    Phase 1 PRESSED-wait will detect the dead iterator and return 2
    per the fatal-triage table.
    """
    try:
        async for event in ptt_iter:
            if event == ptt.PTTEvent.RELEASED:
                return
    finally:
        ptt_release.set()


async def text_loop() -> int:
    """Tier-2-preserved keyboard REPL. ``run()`` dispatches here on ``--text``.

    Exit codes:
        0 — Ctrl+C or EOF at the prompt.
        2 — unrecoverable auth failure mid-session.

    Assumes ``run()`` has already loaded ``.env`` and verified
    ``ANTHROPIC_API_KEY`` is present.
    """
    print(_BANNER)
    messages: list[dict] = []

    while True:
        # --- prompt for a user turn ----------------------------------
        try:
            # sync input() is deliberate — text REPL has no concurrent
            # coroutines and pauses on stdin by design.
            user = input("you > ").strip()
        except (KeyboardInterrupt, EOFError):
            print(_GOODBYE)
            return 0
        if not user:
            continue

        # --- send it and stream the reply ----------------------------
        messages.append({"role": "user", "content": user})
        print("friday > ", end="", flush=True)
        chunks: list[str] = []
        try:
            # NOTE: mid-stream Ctrl+C surfaces as asyncio.CancelledError under
            # asyncio.run(), not KeyboardInterrupt — deferred to #55 with the
            # rest of the voice-pipeline error handling (see PR #63 review).
            async for chunk in stream_tokens(messages, SYSTEM_PROMPT):
                print(chunk, end="", flush=True)
                chunks.append(chunk)
            print()
            messages.append({"role": "assistant", "content": "".join(chunks)})
        except KeyboardInterrupt:
            # Mid-stream interrupt — stay in the loop, drop the pending user
            # turn so history stays consistent with what the model saw.
            print("\n[stopped]")
            messages.pop()
        except AuthenticationError:
            print("\n[auth] API key isn't working. Check your .env and restart.")
            return 2
        except RateLimitError:
            print("\n[rate] Hit the rate limit — give it a moment.")
            messages.pop()
        except APIConnectionError:
            print("\n[net] Can't reach Claude right now — check your connection.")
            messages.pop()
        except Exception as e:  # noqa: BLE001 — final safety net for text loop
            print(f"\n[err] {type(e).__name__}: {e}")
            messages.pop()


async def voice_loop() -> int:
    """Tier-3 push-to-talk pipeline. ``run()`` dispatches here by default.

    One turn: PRESSED → ``audio.capture`` → ``stt.transcribe`` →
    (empty? short-circuit) → ``claude.stream_sentences`` →
    ``tts.synthesize`` → ``audio.playback`` → back to PRESSED wait.
    One ``ptt.events()`` iterator is held for the whole loop; a
    per-turn ``_watch_release`` task is spawned to catch RELEASED
    and torn down at turn end.

    Exit codes:
        0 — Ctrl+C at the idle wait between turns.
        2 — fatal auth across any provider, or the PTT event stream
            dies / exhausts (continuing would tight-loop into the
            same error on the next ``_next_press`` call).

    Assumes ``run()`` has already loaded ``.env`` and verified all
    three keys are present.
    """
    print(_VOICE_BANNER)
    messages: list[dict] = []
    ptt_iter = ptt.events()

    try:
        while True:
            # --- Phase 1: idle wait for PRESSED --------------------
            try:
                async for event in ptt_iter:
                    if event == ptt.PTTEvent.PRESSED:
                        break
                else:
                    # ptt_iter exhausted before PRESSED. NOTE: #55 may
                    # want to add structured PTT-health handling here;
                    # for now, PTT death is fatal (A2 in #53 plan).
                    print("[ptt] event stream ended at idle wait")
                    return 2
            except KeyboardInterrupt:
                print(_GOODBYE)
                return 0
            except Exception as e:  # noqa: BLE001 — PTT death is fatal (A2)
                # NOTE: #55 may introduce structured PTT-health handling
                # (restart listener? exit?) — right now any PTT iterator
                # exception is terminal to avoid tight error loops.
                print(f"[ptt] {type(e).__name__}: {e}")
                return 2

            # Spawn the per-turn RELEASED watcher.
            ptt_release = asyncio.Event()
            watcher_task = asyncio.create_task(
                _watch_release(ptt_iter, ptt_release),
                name="voice-loop-ptt-watcher",
            )

            try:
                # --- Phase 2: transcribe ---------------------------
                try:
                    transcript = await stt.transcribe(audio.capture(), ptt_release)
                except (stt.STTAuthError, stt.STTForceEndTurnUnsupported) as e:
                    print(f"\n[auth] {e}")
                    return 2
                except stt.STTTimeout:
                    print("\n[stt timeout]")
                    continue
                except (stt.STTFatal, stt.STTError) as e:
                    print(f"\n[stt] {e}")
                    continue

                # --- Phase 3: empty-transcript short-circuit -------
                if transcript == "":
                    print("[no speech]")
                    continue

                # --- Phase 4: user turn append ---------------------
                messages.append({"role": "user", "content": transcript})

                # --- Phase 5: Claude + TTS + playback --------------
                collect: list[str] = []
                try:
                    sentence_iter = stream_sentences(messages, SYSTEM_PROMPT)
                    synth_iter = tts.synthesize(_tee(sentence_iter, collect))
                    # NOTE: mid-stream Ctrl+C surfaces as asyncio.CancelledError
                    # under asyncio.run(), not KeyboardInterrupt — deferred to
                    # #55 with the rest of the voice-pipeline error handling.
                    await audio.playback(synth_iter)
                except AuthenticationError:
                    print("\n[auth] API key isn't working. Check your .env and restart.")
                    return 2  # A1: fatal, do NOT pop (match text_loop)
                except tts.TTSAuthError as e:
                    print(f"\n[auth] {e}")
                    return 2  # A1: fatal
                except RateLimitError:
                    print("\n[rate] Hit the rate limit — give it a moment.")
                    messages.pop()
                    continue
                except APIConnectionError:
                    print("\n[net] Can't reach Claude right now — check your connection.")
                    messages.pop()
                    continue
                except tts.TTSError as e:
                    print(f"\n[tts] {e}")
                    messages.pop()
                    continue
                except Exception as e:  # noqa: BLE001 — final safety net for voice loop
                    print(f"\n[err] {type(e).__name__}: {e}")
                    messages.pop()
                    continue

                # --- Phase 6: assistant turn append (clean path) ---
                messages.append({"role": "assistant", "content": "".join(collect)})
            finally:
                # Tear down the per-turn watcher so it doesn't eat the
                # next PRESSED. On RELEASED-seen the watcher has
                # already returned; this cancel is a no-op in that
                # case. On any mid-turn exit (recoverable or fatal)
                # the watcher may still be waiting on ptt_iter —
                # cancel + await.
                if not watcher_task.done():
                    watcher_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await watcher_task
    finally:
        # Close the held PTT iterator on any exit path so pynput's
        # Listener stops cleanly. ``agent.ptt.events`` closes
        # idempotently via its own finally.
        with contextlib.suppress(Exception):
            await ptt_iter.aclose()


async def run() -> int:
    """Entry point for ``python -m agent``. Parses argv and dispatches.

    Flags:
        --text   Run the text REPL instead of the voice pipeline.

    Exit codes:
        0 — clean exit (Ctrl+C at prompt / idle, EOF).
        1 — startup failure (missing API key).
        2 — unrecoverable auth / fatal provider error mid-session.
    """
    # Force UTF-8 on stdout so the em dash in both banners and any
    # non-ASCII in Claude's replies render correctly on Windows
    # consoles (which otherwise default to the system ANSI code page).
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — best-effort; a stdout without
        pass  # reconfigure (rare) still works for ASCII.

    parser = argparse.ArgumentParser(
        prog="python -m agent",
        description="FRIDAY — voice-first conversation loop.",
    )
    parser.add_argument(
        "--text",
        action="store_true",
        help="Run the keyboard REPL instead of the voice pipeline.",
    )
    args = parser.parse_args()

    load_dotenv()

    if args.text:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print(_MISSING_KEY)
            return 1
        return await text_loop()

    missing = [key for key in _VOICE_REQUIRED_KEYS if not os.environ.get(key)]
    if missing:
        print(_voice_missing_keys_message(missing))
        return 1
    return await voice_loop()
