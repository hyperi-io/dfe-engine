#  Project:      dfe-engine
#  File:         tests/unit/test_e2e_harness/test_poll_until.py
#  Purpose:      The e2e readiness poll returns the value its acceptance test takes
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""``poll_until`` waits on truthiness by default, and on ``done`` where a falsy-looking value is not ready."""

from collections.abc import Callable, Iterator

import pytest

from tests.e2e.conftest import poll_until


def _returning(*values: int) -> Callable[[], int]:
    sequence: Iterator[int] = iter(values)
    return lambda: next(sequence)


def test_the_first_truthy_value_is_returned_by_default():
    assert poll_until(_returning(0, 0, 7), timeout=5, interval=0) == 7


def test_done_holds_out_for_the_value_it_accepts():
    """An empty `_json` renders 2 characters, which truthiness would take as filled."""
    lengths = _returning(2, 2, 41)

    assert poll_until(lengths, timeout=5, interval=0, done=lambda n: n > 2) == 41


def test_a_value_done_never_accepts_times_out():
    with pytest.raises(AssertionError, match=r"timed out after 0\.05s waiting for a refill"):
        poll_until(lambda: 2, timeout=0.05, interval=0.01, done=lambda n: n > 2, desc="a refill")
