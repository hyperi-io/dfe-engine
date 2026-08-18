#  Project:      dfe-engine
#  File:         synthetic_data/models.py
#  Purpose:      Shared models + errors for the synthetic data component
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Models for the synthetic data component.

``ColumnHints`` is the authoring surface for reference packs: a meta-schema
column may carry an optional ``synthetic:`` mapping (ignored by the schema
loader, so it is non-breaking) that overrides the heuristic generator for
that column. Hints are applied in priority order: ``static`` > ``values`` >
``templates`` > ``provider`` > ``minimum``/``maximum`` range.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SyntheticDataMode(StrEnum):
    """How events are produced.

    - ``schema`` - reference pack driven by a dfe-schemas meta schema.
    - ``sample`` - lookalike of a logreducer-reduced sample (later stage).
    """

    SCHEMA = "schema"
    SAMPLE = "sample"


class ColumnHints(BaseModel):
    """Per-column generation hints authored in the schema YAML (``synthetic:``).

    Attributes:
        static: Emit this exact value for every event.
        values: Categorical vocabulary to draw from.
        weights: Optional draw weights aligned with ``values``.
        templates: Message templates with ``{placeholder}`` tokens filled from
            the event's entity context (e.g. ``{username}``, ``{ipv4}``).
        provider: Faker provider method name to call (e.g. ``ipv4_public``).
        minimum: Lower bound for numeric generation.
        maximum: Upper bound for numeric generation.
        format: Timestamp rendering - ``iso8601`` (default), ``epoch_s``,
            ``epoch_ms``, or a ``strftime`` pattern.
    """

    static: Any | None = None
    values: list[Any] | None = Field(default=None, min_length=1)
    weights: list[float] | None = Field(
        default=None, description="Draw weights aligned with values"
    )
    templates: list[str] | None = Field(default=None, min_length=1)
    provider: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    format: str | None = None

    @model_validator(mode="after")
    def _check_weights(self) -> ColumnHints:
        """Weights only make sense alongside a matching values list."""
        if self.weights is not None:
            if self.values is None:
                raise ValueError("synthetic data hints: 'weights' requires 'values'")
            if len(self.weights) != len(self.values):
                raise ValueError(
                    f"synthetic data hints: {len(self.weights)} weights for {len(self.values)} values"
                )
        return self


class Scenario(BaseModel):
    """A coherent event shape drawn once per event (schema-level ``synthetic.scenarios``).

    Correlated columns (appname + facility + message in syslog, module +
    dataset + file + message in beats) must not draw independently or the
    event reads as fake. A scenario pins them together: one weighted draw
    per event selects the scenario, whose ``values``/``templates`` override
    per-column hints for the columns it names.

    Attributes:
        name: Label for debugging/tests.
        weight: Relative draw weight among the schema's scenarios.
        values: Column name -> value; a list draws uniformly, a scalar is
            emitted as-is.
        templates: Column name -> template set (same placeholders as
            :class:`ColumnHints`).
    """

    name: str | None = None
    weight: float = Field(default=1.0, gt=0)
    values: dict[str, Any] = Field(default_factory=dict)
    templates: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_templates(self) -> Scenario:
        """A scenario naming a column with an empty template set is an authoring error."""
        for column, templates in self.templates.items():
            if not templates:
                raise ValueError(f"scenario template set for {column!r} is empty")
        return self


class PackInfo(BaseModel):
    """A generatable schema pack (a meta schema under the schemas root)."""

    ref: str = Field(description="Schema ref relative to the schemas root, e.g. meta/syslog")
    provider: str = Field(description="Cloud vocabulary hint inferred from the path")
    columns: int = Field(description="@source columns the generator will fill")
    scenarios: int = Field(default=0, description="Coherent synthetic data scenarios declared")
    hinted_columns: int = Field(default=0, description="Columns carrying synthetic data hints")


class GenerateRequest(BaseModel):
    """Inline generation request (bounded batch, timestamps read as a live tail)."""

    model_config = ConfigDict(populate_by_name=True)

    schema_ref: str = Field(
        alias="schema", description="Schema ref (e.g. meta/syslog) or path under the schemas root"
    )
    version: str | None = Field(default=None, description="Schema version (default: current)")
    count: int = Field(default=100, ge=1, description="Events to generate (capped by settings)")
    seed: int | None = Field(default=None, description="Determinism seed")
    rate_eps: float | None = Field(
        default=None, gt=0, description="Timestamp spacing rate (events/second)"
    )
    tags: dict[str, Any] | None = Field(default=None, description="Extra tags merged into events")
    mark_synthetic: bool = Field(default=True, description="Emit tags.synthetic=true")


class LookalikeRequest(BaseModel):
    """Inline lookalike generation from a sample (the sampler's rows/lines).

    Identity values in the sample (IPs, users, hosts, emails, ...) are never
    replayed - they are synthesised from the entity pool. Enum vocabularies
    and message structure are kept.
    """

    rows: list[dict[str, Any]] | None = Field(
        default=None, description="Parsed sample events (preferred input)"
    )
    lines: list[str] | None = Field(
        default=None, description="Raw sample lines (used when rows is empty)"
    )
    count: int = Field(default=100, ge=1, description="Events to generate (capped by settings)")
    seed: int | None = Field(default=None, description="Determinism seed")
    rate_eps: float | None = Field(
        default=None, gt=0, description="Timestamp spacing rate (events/second)"
    )
    tags: dict[str, Any] | None = Field(default=None, description="Extra tags merged into events")
    mark_synthetic: bool = Field(default=True, description="Emit tags.synthetic=true")


class GenerateResult(BaseModel):
    """Inline generation result."""

    schema_ref: str = Field(serialization_alias="schema")
    version: str | None = None
    count: int = 0
    seed: int | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)


class StreamRequest(BaseModel):
    """Background stream request - posts generated events to an HTTP ingest URL."""

    model_config = ConfigDict(populate_by_name=True)

    schema_ref: str = Field(
        alias="schema", description="Schema ref (e.g. meta/syslog) or path under the schemas root"
    )
    version: str | None = Field(default=None, description="Schema version (default: current)")
    seed: int | None = Field(default=None, description="Determinism seed")
    rate_eps: float | None = Field(
        default=None, gt=0, description="Events/second (Poisson; capped by settings)"
    )
    count: int | None = Field(default=None, ge=1, description="Stop after this many events")
    duration_s: float | None = Field(
        default=None, gt=0, description="Stop after this many seconds (capped by settings)"
    )
    receiver_url: str = Field(description="Ingest URL to POST events to (dfe-receiver)")
    headers: dict[str, str] | None = Field(
        default=None, description="Extra request headers (e.g. receiver auth)"
    )
    ndjson: bool = Field(default=False, description="POST batches as NDJSON instead of JSON")
    batch_max: int = Field(default=10, ge=1, description="Events per POST")
    tags: dict[str, Any] | None = Field(default=None, description="Extra tags merged into events")
    mark_synthetic: bool = Field(default=True, description="Emit tags.synthetic=true")
    wait: float | None = Field(
        default=None, ge=0, description="Seconds to block for inline completion before returning"
    )


class StreamSummary(BaseModel):
    """Terminal result of a stream task."""

    schema_ref: str = Field(serialization_alias="schema")
    receiver_url: str
    emitted: int = 0
    sent: int = 0
    failed: int = 0
    seconds: float = 0.0


class SyntheticDataError(Exception):
    """A generation request that cannot be served (bad schema, bad hint, ...)."""
