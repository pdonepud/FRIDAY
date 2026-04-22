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
import edge_tts
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


def speak(text: str, voice: str = VOICE) -> None:
    """
    Speak text out loud. Blocks until playback finishes.

    Args:
        text:  What to say.
        voice: edge-tts voice name (defaults to config.VOICE).
    """
    if not text.strip():
        return

    # Create temp mp3
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        mp3_path = f.name

    try:
        _generate_audio(text, voice, mp3_path)

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


if __name__ == "__main__":
    print(f"[voice] Testing with voice: {VOICE}")
    speak("Voice system online. FRIDAY is ready.")
    print("[voice] Done.")