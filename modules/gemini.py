"""
gemini.py — FRIDAY's brain.

A minimal wrapper around the Google Gemini REST API. Other modules call
`ask(prompt)` to get a text response back. No SDK dependency — just `requests`.

Test directly:
    python modules/gemini.py
"""

import os
import sys
from typing import Optional

import requests

# Allow running this file directly OR as a module.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import GEMINI_API_KEY


_MODEL = "gemini-2.5-flash-lite"
_ENDPOINT = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{_MODEL}:generateContent"
)
_TIMEOUT_SECONDS = 30


def ask(prompt: str, system: Optional[str] = None, temperature: float = 0.7) -> str:
    """
    Send a prompt to Gemini and return its text response.

    Args:
        prompt:      The user question / instruction.
        system:      Optional system instruction (Gemini's `systemInstruction`).
        temperature: Sampling temperature, 0.0–1.0.

    Returns:
        The plain text response from the model.

    Raises:
        RuntimeError: If the API key is missing, the request fails, or the
                      response payload is malformed.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is missing. Set it in config.py "
            "(get one at https://aistudio.google.com)."
        )

    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": prompt}]}
        ],
        "generationConfig": {
            "temperature": temperature,
        },
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}

    try:
        response = requests.post(
            _ENDPOINT,
            params={"key": GEMINI_API_KEY},
            json=payload,
            timeout=_TIMEOUT_SECONDS,
        )
    except requests.Timeout:
        raise RuntimeError(f"Gemini request timed out after {_TIMEOUT_SECONDS}s.")
    except requests.RequestException as e:
        raise RuntimeError(f"Gemini request failed: {e}")

    if response.status_code in (400, 403):
        raise RuntimeError(
            f"Gemini rejected the request ({response.status_code}). "
            f"Check your API key and that the Generative Language API is enabled. "
            f"Body: {response.text[:300]}"
        )
    if response.status_code == 404:
        raise RuntimeError(
            f"Gemini model '{_MODEL}' not found (404). "
            f"Try switching _MODEL to 'gemini-1.5-flash' in modules/gemini.py."
        )
    if response.status_code == 429:
        raise RuntimeError("Gemini rate limit hit (429). Slow down or wait a minute.")
    if not response.ok:
        raise RuntimeError(
            f"Gemini HTTP {response.status_code}: {response.text[:300]}"
        )

    try:
        data = response.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, ValueError) as e:
        raise RuntimeError(
            f"Malformed Gemini response: {e}. Body: {response.text[:300]}"
        )


if __name__ == "__main__":
    print("Testing Gemini...")
    reply = ask("Say 'FRIDAY brain online' in exactly 4 words.")
    print(reply)
