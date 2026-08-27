"""Entry point for `python -m agent`."""

import sys

from agent.loop import run

if __name__ == "__main__":
    sys.exit(run())
