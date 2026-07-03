#  Project:      dfe-engine
#  File:         sampling/models.py
#  Purpose:      Request/result models + enums for source sampling
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Models for the source sampler.

A "sample" is a small, inspectable slice of a source's events pulled from
ClickHouse (landed ``_json``) or Kafka (a topic), in one of four modes. The
result carries BOTH shapes its consumers need: raw ``lines`` (what the AI
plug-in / logreducer eat) and parsed ``rows`` + discovered ``keys`` (what the
UI charts and downstream APIs want).
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SampleMode(str, Enum):
    """How to pick the sample.

    - ``recent``  - newest rows first (fast tail; ClickHouse ORDER BY ts DESC,
      Kafka reads from the partition high-watermark backwards).
    - ``random``  - uniform-ish random rows (fair distribution, no recency bias).
    - ``smart``   - logreducer representative/diverse sample (default). Gated.
    - ``anomaly`` - logreducer isolation-forest outliers (the weird events). Gated.
    """

    RECENT = "recent"
    RANDOM = "random"
    SMART = "smart"
    ANOMALY = "anomaly"


class SampleBackend(str, Enum):
    """Where the data is read from."""

    CLICKHOUSE = "clickhouse"
    KAFKA = "kafka"


# Modes that drive logreducer: memory-hungry, concurrency-gated, and async by
# default (submit -> poll) so the UI gets a clean in-progress state.
GATED_MODES: frozenset[SampleMode] = frozenset({SampleMode.SMART, SampleMode.ANOMALY})


class SampleRequest(BaseModel):
    """A request to sample a source.

    Provide EITHER a registered ``source`` (its CH table / land topic are
    resolved for you) OR an explicit ``table``/``topic`` (ad-hoc - for a source
    that is not registered yet, e.g. AI onboarding). ``filter`` is a trusted SQL
    predicate (ClickHouse backend only), consistent with the query-authoring
    surface - callers already hold the sampler scope.
    """

    mode: SampleMode = SampleMode.SMART
    backend: SampleBackend = SampleBackend.CLICKHOUSE
    limit: int | None = Field(
        default=None, ge=1, description="Rows to return (defaults to sampler.default_limit)"
    )
    source: str | None = Field(default=None, description="Registered source name")
    table: str | None = Field(
        default=None, description="Explicit ClickHouse table (overrides source's table)"
    )
    topic: str | None = Field(
        default=None, description="Explicit Kafka topic (overrides source's land topic)"
    )
    filter: str | None = Field(
        default=None, description="Trusted SQL WHERE predicate (ClickHouse backend only)"
    )
    since: str | None = Field(
        default=None, description="Lower time bound (ISO 8601) on the timestamp column"
    )
    until: str | None = Field(
        default=None, description="Upper time bound (ISO 8601) on the timestamp column"
    )
    seed: int | None = Field(default=None, description="Seed for random mode (determinism)")
    level: str | None = Field(
        default=None, description="logreducer level override: standard | enhanced | maximum"
    )
    wait: float | None = Field(
        default=None,
        ge=0,
        description="Seconds to block for inline completion before returning pending",
    )


class SampleResult(BaseModel):
    """The sampled data + provenance."""

    mode: SampleMode
    backend: SampleBackend
    source: str | None = None
    target: str = Field(default="", description="Resolved table or topic sampled")
    count: int = Field(default=0, description="Rows returned")
    requested_limit: int = 0
    lines: list[str] = Field(
        default_factory=list, description="Raw event strings (_json / Kafka message values)"
    )
    rows: list[dict[str, Any]] = Field(
        default_factory=list, description="Parsed JSON objects (best-effort; skips non-JSON)"
    )
    keys: list[str] = Field(
        default_factory=list, description="Top-level keys discovered across the sample"
    )
    truncated: bool = Field(
        default=False, description="True when more rows were available than returned"
    )
    stats: dict[str, Any] = Field(
        default_factory=dict,
        description="Mode-specific stats (scan counts, logreducer stop reason)",
    )
    note: str | None = Field(default=None, description="Any caveat about how the sample was taken")


class SamplerError(Exception):
    """A sampling request that cannot be served (bad target, missing dep, ...)."""
