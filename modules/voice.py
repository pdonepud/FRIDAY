"""
voice.py — FRIDAY's voice.

Wraps edge-tts (free Microsoft neural voices) + pygame for playback.
Synchronous `speak()` blocks until audio finishes playing.

Test this module directly:
    python modules/voice.py
"""

import asyncio
import os
import sys
import tempfile
import threading
import edge_tts
import keyboard
import pygame

# Allow running this file directly OR as a module
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import VOICE


def _generate_audio(text: str, voice: str, out_path: str) -> None:
    """Async helper: generate mp3 from text via edge-tts."""
    async def _run():
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(out_path)
    asyncio.run(_run())


def _generate_mp3(text: str, voice: str = VOICE) -> str:
    """
    Generate MP3 file from text and return the temporary file path.

    Args:
        text: Text to convert to speech
        voice: edge-tts voice name

    Returns:
        Path to temporary MP3 file (caller must clean up)
    """
    if not text.strip():
        raise ValueError("Text cannot be empty")

    # Create temp mp3
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        mp3_path = f.name

    _generate_audio(text, voice, mp3_path)
    return mp3_path


def speak(text: str, voice: str = VOICE) -> None:
    """
    Speak text out loud. Blocks until playback finishes.

    Args:
        text:  What to say.
        voice: edge-tts voice name (defaults to config.VOICE).
    """
    if not text.strip():
        return

    mp3_path = _generate_mp3(text, voice)

    try:
        # Init pygame mixer lazily
        if not pygame.mixer.get_init():
            pygame.mixer.init()

        pygame.mixer.music.load(mp3_path)
        pygame.mixer.music.play()

        # Block until done
        while pygame.mixer.music.get_busy():
            pygame.time.Clock().tick(10)

        pygame.mixer.music.unload()

    finally:
        # Clean up temp file
        try:
            os.remove(mp3_path)
        except OSError:
            pass


def speak_interruptible(text: str, voice: str = VOICE, stop_key: str = "esc") -> bool:
    """
    Speak text out loud with the ability to interrupt playback via hotkey.

    Args:
        text: What to say
        voice: edge-tts voice name (defaults to config.VOICE)
        stop_key: Key to press to stop playback (default: "esc")

    Returns:
        True if completed normally, False if interrupted
    """
    if not text.strip():
        return True

    mp3_path = _generate_mp3(text, voice)

    try:
        # Init pygame mixer lazily
        if not pygame.mixer.get_init():
            pygame.mixer.init()

        # Set up interruption mechanism
        stop_event = threading.Event()
        hook = None

        try:
            hook = keyboard.add_hotkey(stop_key, lambda: stop_event.set())
        except Exception as e:
            print(f"[voice] Warning: hotkey registration failed ({e}). Audio will play uninterruptibly.")
            hook = None

        print(f"[voice] Press {stop_key.upper()} to stop")

        pygame.mixer.music.load(mp3_path)
        pygame.mixer.music.play()

        # Loop while audio is playing
        while pygame.mixer.music.get_busy():
            if stop_event.is_set():
                pygame.mixer.music.stop()
                return False  # Interrupted
            pygame.time.Clock().tick(10)

        pygame.mixer.music.unload()
        return True  # Completed normally

    finally:
        # Clean up
        if hook is not None:
            try:
                keyboard.remove_hotkey(hook)
            except:
                pass

        try:
            os.remove(mp3_path)
        except OSError:
            pass


if __name__ == "__main__":
    print(f"[voice] Testing with voice: {VOICE}")

    # Existing test: speak() still works
    print("[voice] Test 1: regular speak (uninterruptible)")
    speak("This is the regular speak function. It plays to completion.")

    print()
    print("[voice] Test 2: interruptible speak — press ESC within ~5 seconds to test interruption.")
    completed = speak_interruptible(
        "This is the interruptible function. Press escape any time you want to stop me. "
        "I will keep talking for a while if you do not stop me. The quick brown fox jumps over the lazy dog. "
        "I am now reciting filler so you have time to test the stop key. The rain in Spain stays mainly on the plain. "
        "Buffalo buffalo Buffalo buffalo buffalo buffalo Buffalo buffalo. "
        "If you have not pressed escape yet, you should soon."
    )
    if completed:
        print("[voice] Test 2 finished naturally (no interruption).")
    else:
        print("[voice] Test 2 was interrupted — interrupt mechanism working!")

    print("[voice] Done.")