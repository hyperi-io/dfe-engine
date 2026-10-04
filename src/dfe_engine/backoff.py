#  Project:      dfe-engine
#  File:         backoff.py
#  Purpose:      Jitter for a retry's wait, so peers that failed together retry apart
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The jitter every retry in the engine spreads its back-off with.

A fleet that lost a dependency together comes back together, and a fixed wait
keeps every replica dialling it in step. Each wait is drawn from the upper half
of the nominal back-off, so it never exceeds a cap the caller set.
"""

import random
import time


def jittered(seconds: float) -> float:
    """A wait between half of *seconds* and all of it."""
    return random.uniform(seconds / 2, seconds)  # noqa: S311 - schedules a retry, not crypto


def jittered_sleep(seconds: float) -> None:
    """Sleep a jittered share of *seconds*."""
    time.sleep(jittered(seconds))
