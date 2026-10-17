#  Project:      dfe-engine
#  File:         tests/support/slow_store.py
#  Purpose:      A secret store whose read waits at a gate, as a slow OpenBao does
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A secret store whose read waits at a gate, as an OpenBao that is slow to answer does."""

import threading

from dfe_engine.secrets import DfeSecrets
from tests.support.event_loop import HELD_SECONDS


class SlowSecretStore(DfeSecrets):
    """A store that answers every read with ``value``, once the test opens ``gate``.

    A read that reaches the store sets ``arrived`` and waits at ``gate``. A read made
    on the event loop holds the loop too, so the wait times out and records ``False``.
    ``read_threads`` holds the thread id of each read.
    """

    def __init__(self, value: str) -> None:
        """Hold no backend: every read is answered here."""
        self.arrived = threading.Event()
        self.gate = threading.Event()
        self.gate_opened_by_test: list[bool] = []
        self.read_threads: list[int] = []
        self._value = value

    def get(self, path: str) -> str:
        """Wait at the gate, then answer."""
        self.read_threads.append(threading.get_ident())
        self.arrived.set()
        self.gate_opened_by_test.append(self.gate.wait(timeout=HELD_SECONDS))
        return self._value
