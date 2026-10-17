#  Project:      dfe-engine
#  File:         tests/support/event_loop.py
#  Purpose:      Show that a blocking call made from async code leaves the event loop running
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Show that a blocking call made from async code leaves the event loop running.

A test double on the blocking side sets ``arrived`` when the call reaches it, then waits
for ``gate``. :func:`ticks_while_held` counts loop iterations while the call is held, and
then opens the gate. A call made on the loop itself holds the loop too, so the double's
own wait times out before the gate opens and the count stays near zero.
"""

import asyncio
import threading
import time
from collections.abc import Callable

# Longest any wait here lasts, so a test that fails does so in seconds and never hangs.
HELD_SECONDS = 5


async def _until(condition: Callable[[], bool]) -> None:
    """Yield to the loop until *condition* holds or the wait limit passes."""
    deadline = time.monotonic() + HELD_SECONDS
    while not condition() and time.monotonic() < deadline:
        await asyncio.sleep(0.005)


async def ticks_while_held(
    *, arrived: threading.Event, gate: threading.Event, wanted: int = 5
) -> int:
    """Count event-loop iterations while a blocking call is held at *gate*, then open the gate.

    Args:
        arrived: Set by the double when the blocking call reaches it.
        gate: The double waits on it, and this opens it once *wanted* iterations ran.
        wanted: Iterations to wait for after the call arrives.

    Returns:
        The iterations counted before the gate opened.
    """
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while not gate.is_set():
            ticks += 1
            await asyncio.sleep(0.005)

    ticker = asyncio.create_task(tick())
    await _until(arrived.is_set)
    await _until(lambda: ticks >= wanted)
    counted = ticks
    gate.set()
    await ticker
    return counted
