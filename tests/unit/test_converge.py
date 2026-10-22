#  Project:      dfe-engine
#  File:         tests/unit/test_converge.py
#  Purpose:      A derived write runs again until a pass changes nothing, within its bound
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""converge: the C5 loop the bearer sync runs under."""

from dfe_engine.store.documents import CONVERGE_PASSES, converge


def _passes(*changes: list[str]):
    """A write returning each of *changes* in turn, then nothing, and counting its calls."""
    calls: list[int] = []
    queue = list(changes)

    def write() -> list[str]:
        calls.append(1)
        return queue.pop(0) if queue else []

    return write, calls


def test_it_stops_at_the_first_pass_that_changes_nothing():
    write, calls = _passes(["http"], ["otlp"])

    assert converge(write) == ["http", "otlp"]
    assert len(calls) == 3


def test_a_first_pass_that_changes_nothing_is_the_only_one():
    write, calls = _passes()

    assert converge(write) == []
    assert len(calls) == 1


def test_it_gives_up_after_its_passes():
    write, calls = _passes(*[["http"]] * (CONVERGE_PASSES + 2))

    assert converge(write) == ["http"] * CONVERGE_PASSES
    assert len(calls) == CONVERGE_PASSES
