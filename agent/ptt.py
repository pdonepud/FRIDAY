"""Async push-to-talk event stream over pynput.

Per ADR-0003 §Input: this is the ONLY module in the codebase that
imports ``pynput``. Everything else consumes ``PTTEvent`` values from
the ``events()`` async generator here.

The async-bridging pattern mirrors ``agent.audio.capture()``: pynput's
``Listener`` fires ``on_press`` / ``on_release`` callbacks on its own
thread; those callbacks schedule an ``_enqueue`` helper on the event
loop via ``call_soon_threadsafe``, which puts events onto a bounded
``asyncio.Queue`` with drop-oldest semantics. The async generator
drains the queue via ``await queue.get()``. Cancellation of the
consuming task runs a ``finally`` block that stops the listener.

Layout caveat: on many non-US keyboard layouts Right Alt is AltGr and
gets consumed by the OS for character composition before pynput sees
it. Windows-first (US layout) is verified per ADR-0003; other layouts
are unverified for FRIDAY. Users on non-US layouts will need a
different PTT key — filed as a follow-up if it comes up.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from enum import IntEnum

from pynput.keyboard import Key, Listener

__all__ = [
    "EVENT_QUEUE_MAX",
    "PTT_KEY",
    "PTTEvent",
    "events",
]

_log = logging.getLogger(__name__)


class PTTEvent(IntEnum):
    """Push-to-talk state signals emitted by the ``events()`` async generator."""

    PRESSED = 1
    RELEASED = 2


# pynput exposes Right Alt as ``Key.alt_r``. Explicit constant avoids
# scattering the raw pynput enum through consumer code and gives a
# single point of change if the hotkey becomes configurable later.
PTT_KEY = Key.alt_r

# Bounded event queue: PTT events are rare (human keypress cadence),
# so 32 is generous headroom. If it saturates, the consumer is broken
# or stalled — drop-oldest with a warning rather than silent unbounded
# growth, matching ADR-0003's real-time-buffering principle from
# ``agent.audio.capture()``.
#
# Pair-integrity note: PTT events come as ordered PRESSED/RELEASED
# pairs. Under saturation, drop-oldest may hand the consumer a
# ``RELEASED`` without a matching earlier ``PRESSED``. Theoretical at
# 32-deep human cadence, but downstream state machines should still be
# defensive about a leading ``RELEASED`` — the warning log is the
# signal that this could have happened.
EVENT_QUEUE_MAX = 32


async def events() -> AsyncIterator[PTTEvent]:
    """Yield PRESSED / RELEASED events for the PTT key (Right Alt).

    Non-PTT keys are ignored. OS key-repeat is suppressed: exactly one
    ``PRESSED`` per physical press, one ``RELEASED`` per release.
    Orphan releases (``RELEASED`` without a preceding ``PRESSED`` this
    generator emitted) are dropped silently.

    Runs until the consuming task is cancelled. The pynput ``Listener``
    is stopped cleanly in a ``finally`` block regardless of exit path.
    No stop-event parameter — cancellation is the mechanism, matching
    ADR-0003's asyncio-first shape.

    When the consumer falls behind and the queue saturates, the oldest
    event is dropped with a warning log. Queue depth is bounded at
    ``EVENT_QUEUE_MAX``. See the note on ``EVENT_QUEUE_MAX`` about
    pair-integrity risk under saturation.

    Yields:
        ``PTTEvent.PRESSED`` or ``PTTEvent.RELEASED``.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[PTTEvent] = asyncio.Queue(maxsize=EVENT_QUEUE_MAX)
    last_emitted: PTTEvent | None = None

    def _enqueue(event: PTTEvent) -> None:
        # Runs on the event loop (scheduled via call_soon_threadsafe).
        # Safe to touch the asyncio queue here.
        if queue.full():
            try:
                queue.get_nowait()  # drop oldest
            except asyncio.QueueEmpty:
                pass  # race with consumer; harmless
            _log.warning(
                "PTT event queue saturated (max=%d); dropped oldest. "
                "Consumer stalled or broken; PRESSED/RELEASED pair "
                "integrity may be broken across the drop.",
                EVENT_QUEUE_MAX,
            )
        queue.put_nowait(event)

    def _on_press(key) -> None:  # pynput Listener thread
        nonlocal last_emitted
        if key != PTT_KEY:
            return
        if last_emitted == PTTEvent.PRESSED:
            return  # OS key-repeat; suppress
        last_emitted = PTTEvent.PRESSED
        loop.call_soon_threadsafe(_enqueue, PTTEvent.PRESSED)

    def _on_release(key) -> None:  # pynput Listener thread
        nonlocal last_emitted
        if key != PTT_KEY:
            return
        if last_emitted != PTTEvent.PRESSED:
            return  # release without a prior PRESSED we emitted; suppress
        last_emitted = PTTEvent.RELEASED
        loop.call_soon_threadsafe(_enqueue, PTTEvent.RELEASED)

    listener = Listener(on_press=_on_press, on_release=_on_release)
    try:
        listener.start()
        while True:
            yield await queue.get()
    finally:
        listener.stop()


# --- __main__ helpers ---------------------------------------------------
# The ``if __name__ == "__main__":`` dispatch below carries a
# ``# pragma: no cover`` per the precedent set in #48 (see
# ``agent.audio``): CLI dispatch is not worth mocking with subprocesses.
# The ``_main`` coroutine itself stays covered by a unit test with a
# monkey-patched ``events``.


async def _main() -> None:
    """Print each PTT event as it arrives. Runs until cancelled."""
    async for event in events():
        print(event.name.lower())


if __name__ == "__main__":  # pragma: no cover
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        sys.exit(0)
