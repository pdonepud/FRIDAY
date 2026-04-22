"""
qa.py — FRIDAY's question-and-answer pipeline.

Takes a question, routes it through Gemini with a FRIDAY-flavored system
prompt, prints the answer, and speaks it aloud.

Test directly:
    python modules/qa.py "What's the weather like on Mars?"
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import USER_NAME
from modules.gemini import ask
from modules.voice import speak


FRIDAY_SYSTEM_PROMPT = f"""You are FRIDAY, {USER_NAME}'s personal AI assistant — \
inspired by Tony Stark's FRIDAY. Address {USER_NAME} by name when it feels \
natural. Be witty, confident, and concise.

Your answers will be spoken aloud, so:
- Keep responses to 2-3 sentences MAX, unless {USER_NAME} explicitly asks for detail.
- Plain spoken English only. No markdown, no bullet points, no code blocks, no headers.
- Never say "As an AI", "I'm just a language model", or any similar disclaimer.
- Don't hedge endlessly. State things directly with personality."""


def answer(question: str, speak_aloud: bool = True) -> str:
    """
    Ask FRIDAY a question, print the exchange, and optionally speak the reply.

    Args:
        question:    The user's question.
        speak_aloud: If True, speak the reply via TTS.

    Returns:
        The text of FRIDAY's reply (or the fallback line on error).
    """
    print(f"[You]    {question}")

    try:
        reply = ask(question, system=FRIDAY_SYSTEM_PROMPT, temperature=0.6).strip()
    except Exception as e:
        print(f"[error]  {e}")
        reply = f"Sorry {USER_NAME}, my brain's offline right now."

    print(f"[FRIDAY] {reply}")

    if speak_aloud:
        speak(reply)

    return reply


if __name__ == "__main__":
    question = sys.argv[1] if len(sys.argv) > 1 else "What should I focus on today?"
    answer(question)
