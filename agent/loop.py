"""Tier 1 conversation loop — text-only REPL over Claude.

Run with `python -m agent` from the repo root.
"""

import os

from dotenv import load_dotenv

from agent.claude import (
    APIConnectionError,
    AuthenticationError,
    RateLimitError,
    stream_reply,
)
from agent.system_prompt import SYSTEM_PROMPT


_BANNER: str = "FRIDAY — Tier 1 baseline. Type to talk. Ctrl+C to exit."
_MISSING_KEY: str = (
    "ANTHROPIC_API_KEY isn't set. "
    "Copy .env.example to .env and add your key."
)
_GOODBYE: str = "\n[goodbye]"


def run() -> int:
    """Run the FRIDAY REPL. Returns a process exit code.

    Exit codes:
        0 — clean exit via Ctrl+C or EOF at the prompt.
        1 — startup failure (missing ANTHROPIC_API_KEY).
        2 — unrecoverable auth failure mid-session (bad API key).
    """
    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(_MISSING_KEY)
        return 1

    print(_BANNER)
    messages: list[dict] = []

    while True:
        # --- prompt for a user turn ----------------------------------
        try:
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
            for chunk in stream_reply(messages, SYSTEM_PROMPT):
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
        except Exception as e:  # noqa: BLE001 — final safety net for Tier 1
            print(f"\n[err] {type(e).__name__}: {e}")
            messages.pop()
