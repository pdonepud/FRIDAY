"""Tests for the push-to-talk event stream (``agent.ptt``).

No test constructs a real ``pynput.keyboard.Listener`` — the ``Listener``
class is replaced via ``monkeypatch`` with a fake that captures the
``on_press`` / ``on_release`` callbacks so tests can invoke them
synchronously to drive the state machine deterministically. This
mirrors the ``_BurstingFakeInputStream`` pattern from
``tests/test_audio.py``.

Manual hardware verification lives in ``python -m agent.ptt`` (press
Right Alt, watch stdout, Ctrl+C exits).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from enum import IntEnum

import pytest
from pynput.keyboard import Key, KeyCode

import agent.ptt
from agent.ptt import EVENT_QUEUE_MAX, PTT_KEY, PTTEvent, events

# --- Bundle 1: PTTEvent sanity ------------------------------------------


def test_ptt_event_values_and_distinctness():
    assert PTTEvent.PRESSED == 1
    assert PTTEvent.RELEASED == 2
    assert PTTEvent.PRESSED != PTTEvent.RELEASED


def test_ptt_event_is_intenum():
    assert issubclass(PTTEvent, IntEnum)


# --- Bundle 2: constants sanity -----------------------------------------


def test_ptt_key_is_right_alt():
    assert PTT_KEY == Key.alt_r


def test_event_queue_max_value_and_shape():
    assert EVENT_QUEUE_MAX == 32
    assert isinstance(EVENT_QUEUE_MAX, int)
    assert EVENT_QUEUE_MAX > 0


# --- Bundle 3: API shape ------------------------------------------------


def test_events_is_async_generator_function():
    assert inspect.isasyncgenfunction(events)


# --- Fake Listener + fixture --------------------------------------------


class _FakeListener:
    """Test double for ``pynput.keyboard.Listener``.

    Captures the ``on_press`` / ``on_release`` callbacks the module
    passed at construction. ``start()`` and ``stop()`` are recorded as
    flags for cleanup assertions; the callbacks are invoked directly by
    tests (synchronously, from the test's thread) to drive the state
    machine.
    """

    def __init__(self, on_press=None, on_release=None):
        self.on_press = on_press
        self.on_release = on_release
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def fake_listener(monkeypatch):
    """Install ``_FakeListener`` as ``agent.ptt.Listener``.

    Returns a one-element list that receives the ``_FakeListener``
    instance once ``events()`` constructs it. Tests use
    ``fake_listener[0]`` after driving the async generator.
    """
    captured: list[_FakeListener] = []

    def _factory(on_press=None, on_release=None):
        listener = _FakeListener(on_press=on_press, on_release=on_release)
        captured.append(listener)
        return listener

    monkeypatch.setattr(agent.ptt, "Listener", _factory)
    return captured


async def _start_gen(gen):
    """Advance ``events()`` past listener construction and the first ``await``.

    Schedules the first ``__anext__`` and yields once so the generator
    runs up to ``await queue.get()``. Returns the started task so the
    caller can await the first event on it.
    """
    task = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0)
    return task


# NOTE: don't try to "peek" via a cancelled __anext__ task — cancelling
# a task awaiting gen.__anext__() propagates CancelledError into the
# generator, runs its finally block, and closes it. Assert absence by
# injecting a subsequent known-good event and asserting *its* identity
# instead (see the OS key-repeat and non-PTT-key tests).


# --- Bundle 4-5: PRESSED / RELEASED on Right Alt ------------------------


async def test_emits_pressed_on_right_alt_press(fake_listener):
    gen = events()
    task = await _start_gen(gen)
    listener = fake_listener[0]

    listener.on_press(Key.alt_r)

    event = await asyncio.wait_for(task, timeout=1.0)
    assert event == PTTEvent.PRESSED
    assert listener.started is True
    await gen.aclose()


async def test_emits_released_after_press_on_right_alt(fake_listener):
    gen = events()
    task = await _start_gen(gen)
    listener = fake_listener[0]

    # RELEASED is suppressed without a prior PRESSED, so press first.
    listener.on_press(Key.alt_r)
    first = await asyncio.wait_for(task, timeout=1.0)
    assert first == PTTEvent.PRESSED

    task2 = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0)
    listener.on_release(Key.alt_r)
    second = await asyncio.wait_for(task2, timeout=1.0)
    assert second == PTTEvent.RELEASED

    await gen.aclose()


# --- Bundle 6: ignore non-PTT keys --------------------------------------


async def test_ignores_non_ptt_keys(fake_listener):
    """Non-PTT key events must never surface via events()."""
    gen = events()
    task = await _start_gen(gen)
    listener = fake_listener[0]

    # Fire a spread of non-PTT keys: named key, char, and LEFT alt
    # (distinct from alt_r).
    listener.on_press(Key.space)
    listener.on_release(Key.space)
    listener.on_press(KeyCode.from_char("a"))
    listener.on_release(KeyCode.from_char("a"))
    listener.on_press(Key.alt)
    listener.on_release(Key.alt)

    # Now fire the actual PTT key. If any non-PTT event leaked through,
    # the first event we receive would not be PRESSED from Right Alt.
    listener.on_press(Key.alt_r)

    event = await asyncio.wait_for(task, timeout=1.0)
    assert event == PTTEvent.PRESSED, (
        f"non-PTT key leaked into events(): expected first event to be "
        f"PRESSED from Right Alt, got {event!r}"
    )
    await gen.aclose()


# --- Bundle 7: suppress OS key-repeat -----------------------------------


async def test_suppresses_os_key_repeat(fake_listener):
    """Three consecutive on_press for the same key emit exactly one PRESSED."""
    gen = events()
    task = await _start_gen(gen)
    listener = fake_listener[0]

    # OS key-repeat: three consecutive on_press for the held key.
    listener.on_press(Key.alt_r)
    listener.on_press(Key.alt_r)
    listener.on_press(Key.alt_r)

    first = await asyncio.wait_for(task, timeout=1.0)
    assert first == PTTEvent.PRESSED

    # If any extra PRESSED sneaked through, the next event would be
    # PRESSED instead of RELEASED. Fire on_release and assert that
    # the very next event is RELEASED — passes only if key-repeat
    # suppression really did drop the two extra PRESSEDs.
    task2 = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0)
    listener.on_release(Key.alt_r)
    second = await asyncio.wait_for(task2, timeout=1.0)
    assert second == PTTEvent.RELEASED, (
        f"OS key-repeat leaked: expected RELEASED next, got {second!r}"
    )

    await gen.aclose()


# --- Bundle 8: suppress orphan release ----------------------------------


async def test_suppresses_orphan_release(fake_listener):
    """RELEASED with no prior PRESSED must be suppressed."""
    gen = events()
    task = await _start_gen(gen)
    listener = fake_listener[0]

    # Two orphan releases (no prior press).
    listener.on_release(Key.alt_r)
    listener.on_release(Key.alt_r)

    # Then a valid press. If the orphan releases had leaked through,
    # the first event we receive would be RELEASED, not PRESSED.
    listener.on_press(Key.alt_r)

    event = await asyncio.wait_for(task, timeout=1.0)
    assert event == PTTEvent.PRESSED, f"orphan release leaked: expected PRESSED, got {event!r}"
    await gen.aclose()


# --- Bundle 9: PRESS → RELEASE → PRESS ----------------------------------


async def test_press_release_press_emits_three_events(fake_listener):
    """State machine resets after RELEASED so subsequent PRESS fires."""
    gen = events()
    task = await _start_gen(gen)
    listener = fake_listener[0]

    listener.on_press(Key.alt_r)
    a = await asyncio.wait_for(task, timeout=1.0)
    assert a == PTTEvent.PRESSED

    task2 = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0)
    listener.on_release(Key.alt_r)
    b = await asyncio.wait_for(task2, timeout=1.0)
    assert b == PTTEvent.RELEASED

    task3 = asyncio.create_task(gen.__anext__())
    await asyncio.sleep(0)
    listener.on_press(Key.alt_r)
    c = await asyncio.wait_for(task3, timeout=1.0)
    assert c == PTTEvent.PRESSED

    await gen.aclose()


# --- Bundle 10: cancellation stops listener + surfaces CancelledError ---


async def test_cancellation_stops_listener_and_surfaces_cleanly(fake_listener):
    async def _consume():
        async for _ in events():
            pass

    task = asyncio.create_task(_consume())
    await asyncio.sleep(0.05)  # let events() run up to await queue.get()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(fake_listener) == 1
    assert fake_listener[0].stopped is True


# --- Bundle 11: drop-oldest on queue saturation -------------------------


async def test_drop_oldest_on_queue_saturation(fake_listener, caplog):
    """Injected − delivered == warnings logged (Preetam's cleanest invariant).

    Injects 40 alternating RELEASED/PRESSED events with the state
    machine already at ``last_emitted=PRESSED``; drains everything
    that survived. The queue's capacity minus the injected count is
    exactly the warning-log count.
    """
    caplog.set_level(logging.WARNING, logger="agent.ptt")

    gen = events()
    task = await _start_gen(gen)
    listener = fake_listener[0]

    # Emit one PRESSED so the state machine's ``last_emitted`` is
    # PRESSED before we start alternating RELEASE/PRESS pairs (each
    # pair now emits two events, not one).
    listener.on_press(Key.alt_r)
    first = await asyncio.wait_for(task, timeout=1.0)
    assert first == PTTEvent.PRESSED

    # Inject 40 events. Generator is paused at ``yield`` so nothing
    # drains from the queue during this loop — all 40 accumulate,
    # first 32 fit, next 8 trigger drop-oldest + warning.
    n_injected = 0
    for _ in range(20):
        listener.on_release(Key.alt_r)
        listener.on_press(Key.alt_r)
        n_injected += 2

    # Yield so the loop processes the 40 scheduled ``_enqueue`` calls.
    await asyncio.sleep(0.1)

    # Drain everything currently in the queue. `wait_for` on
    # `gen.__anext__` here does surface the cancellation into the gen
    # on timeout — but that's the *end* of this test's use of the gen,
    # so subsequent `aclose()` is a no-op cleanup.
    drained: list[PTTEvent] = []
    for _ in range(EVENT_QUEUE_MAX + 5):
        try:
            event = await asyncio.wait_for(gen.__anext__(), timeout=0.05)
        except (TimeoutError, StopAsyncIteration):
            break
        drained.append(event)

    await gen.aclose()

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "PTT event queue saturated" in r.getMessage()
    ]

    # The core invariant Preetam suggested:
    assert n_injected - len(drained) == len(warnings), (
        f"invariant broken: injected={n_injected}, drained={len(drained)}, warnings={len(warnings)}"
    )
    # Corollaries — the queue never held more than EVENT_QUEUE_MAX, and
    # overflow count equals the warning count.
    assert len(drained) == EVENT_QUEUE_MAX
    assert len(warnings) == n_injected - EVENT_QUEUE_MAX


# --- Bundle 12: _main helper coverage -----------------------------------


async def test_main_prints_each_event(capsys, monkeypatch):
    """``_main`` iterates ``events()`` and prints each event's name."""

    async def _fake_events():
        yield PTTEvent.PRESSED
        yield PTTEvent.RELEASED

    monkeypatch.setattr(agent.ptt, "events", _fake_events)
    await agent.ptt._main()

    captured = capsys.readouterr()
    assert captured.out == "pressed\nreleased\n"
