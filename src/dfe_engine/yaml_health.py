#  Project:      dfe-engine
#  File:         yaml_health.py
#  Purpose:      The YAML writer's refusals: a labelled counter and the targets left degraded
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the YAML writer reports when it refuses a write.

A refused write leaves the old content in place, so nothing on disk or in the
deploy repo shows it happened. Each refusal is counted on
``yaml_write_failures_total`` by reason, and leaves its target degraded until a
later write of that target goes through. ``GET /api/v1/system/status`` reports
the degraded targets; readiness ignores them, because the content still served is
the last content that wrote cleanly.

The writer is called from every store without an app in reach, so this state is
per process. The engine binds its metrics manager at app creation; with none
bound, the counter records nothing, which is the state the CLI runs in.
"""

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

WRITE_FAILURES = "yaml_write_failures_total"

FailureReason = Literal["dump", "verify"]
"""Why a write was refused.

- ``dump``: turning the data into YAML raised.
- ``verify``: the YAML produced does not read back as the data, or is not UTF-8.
"""


class YamlWriteMetrics:
    """The writer's counter, or a no-op when no backend is wired.

    Args:
        manager: a scalo ``MetricsManager`` (anything exposing ``counter``). ``None``
            means no backend, and every record method returns without doing anything.
    """

    def __init__(self, manager: Any | None = None) -> None:
        self._manager = manager
        if manager is None:
            return
        self._failures = manager.counter(
            WRITE_FAILURES, "YAML writes refused, the old content kept, by reason", ["reason"]
        )

    def failure(self, reason: FailureReason) -> None:
        """Record one refused write."""
        if self._manager is None:
            return
        self._failures.labels(reason=reason).inc()


@dataclass(frozen=True, slots=True)
class DegradedWrite:
    """A target whose last write was refused.

    Attributes:
        target: The file path, or ``deploy-repo:<path>`` for a deploy-repo file.
        reason: Why the latest refusal happened.
        error: The exception type behind it. Never its message, which can quote the data.
        since: RFC 3339 time of the first refusal since the target last wrote.
    """

    target: str
    reason: FailureReason
    error: str
    since: str


class WriteHealth:
    """This process's refused writes, one entry per target until that target writes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._degraded: dict[str, DegradedWrite] = {}
        self._metrics = YamlWriteMetrics()

    def bind(self, metrics: YamlWriteMetrics) -> None:
        """Count later refusals on *metrics*."""
        with self._lock:
            self._metrics = metrics

    def refused(self, target: str | None, reason: FailureReason, error: str) -> None:
        """Count a refusal and, when it names a target, leave that target degraded."""
        with self._lock:
            self._metrics.failure(reason)
            if target is None:
                return
            earlier = self._degraded.get(target)
            since = earlier.since if earlier else datetime.now(UTC).isoformat()
            self._degraded[target] = DegradedWrite(target, reason, error, since)

    def written(self, target: str) -> None:
        """Clear *target*: its content wrote cleanly."""
        with self._lock:
            self._degraded.pop(target, None)

    def degraded(self) -> list[DegradedWrite]:
        """Every degraded target, sorted by target."""
        with self._lock:
            return sorted(self._degraded.values(), key=lambda entry: entry.target)


_HEALTH = WriteHealth()


def write_health() -> WriteHealth:
    """The process's write health, shared by every YAML write."""
    return _HEALTH


__all__ = [
    "WRITE_FAILURES",
    "DegradedWrite",
    "FailureReason",
    "WriteHealth",
    "YamlWriteMetrics",
    "write_health",
]
