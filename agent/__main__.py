"""Entry point for `python -m agent`."""

import asyncio
import sys

from agent.loop import run

if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
