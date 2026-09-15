#  Project:      dfe-engine
#  File:         tests/hunt_runner/conftest.py
#  Purpose:      A metrics manager that records instead of exporting
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""scalo's metrics surface, recording every observation to a list.

``HuntRunnerMetrics`` is built over scalo's ``MetricsManager``, which stands up an
OTel meter provider and a periodic exporter. These tests want the REAL
``HuntRunnerMetrics`` - the instruments it registers and the values it records -
with neither of those, so they hand it this instead.

The namespace (``dfe_``) is applied by scalo's own manager, so the names seen here
are the bare ones this package asks for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest


@dataclass(frozen=True)
class Observation:
    """One recorded observation: which instrument, which labels, what value."""

    metric: str
    labels: dict[str, str]
    op: str
    value: float


class _Instrument:
    """A counter, gauge or histogram with the prometheus-style surface scalo exposes."""

    def __init__(
        self,
        name: str,
        records: list[Observation],
        labels: dict[str, str] | None = None,
    ) -> None:
        self._name = name
        self._records = records
        self._labels = labels or {}

    def labels(self, **labels: Any) -> _Instrument:
        return _Instrument(self._name, self._records, labels)

    def inc(self, amount: float = 1) -> None:
        self._records.append(Observation(self._name, self._labels, "inc", amount))

    def observe(self, value: float) -> None:
        self._records.append(Observation(self._name, self._labels, "observe", value))

    def set(self, value: float) -> None:
        self._records.append(Observation(self._name, self._labels, "set", value))


class RecordingManager:
    """Stands in for ``scalo.metrics.MetricsManager``."""

    def __init__(self) -> None:
        self.registered: list[tuple[str, str]] = []
        self.observations: list[Observation] = []

    def counter(self, name: str, description: str, labels: list[str] | None = None) -> _Instrument:
        return self._register("counter", name)

    def gauge(self, name: str, description: str, labels: list[str] | None = None) -> _Instrument:
        return self._register("gauge", name)

    def histogram(
        self,
        name: str,
        description: str,
        labels: list[str] | None = None,
        buckets: tuple[float, ...] | None = None,
    ) -> _Instrument:
        return self._register("histogram", name)

    def _register(self, kind: str, name: str) -> _Instrument:
        self.registered.append((kind, name))
        return _Instrument(name, self.observations)

    def observed(self, metric: str) -> list[Observation]:
        """Every observation recorded against one instrument, in order."""
        return [o for o in self.observations if o.metric == metric]


@pytest.fixture
def manager() -> RecordingManager:
    """A recording metrics manager to build a real ``HuntRunnerMetrics`` over."""
    return RecordingManager()
