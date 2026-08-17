#  Project:      dfe-engine
#  File:         datagen/models.py
#  Purpose:      Shared models + errors for the datagen component
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Models for the datagen component.

``ColumnHints`` is the authoring surface for reference packs: a meta-schema
column may carry an optional ``datagen:`` mapping (ignored by the schema
loader, so it is non-breaking) that overrides the heuristic generator for
that column. Hints are applied in priority order: ``static`` > ``values`` >
``templates`` > ``provider`` > ``minimum``/``maximum`` range.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class DatagenMode(StrEnum):
    """How events are produced.

    - ``schema`` - reference pack driven by a dfe-schemas meta schema.
    - ``sample`` - lookalike of a logreducer-reduced sample (later stage).
    """

    SCHEMA = "schema"
    SAMPLE = "sample"


class ColumnHints(BaseModel):
    """Per-column generation hints authored in the schema YAML (``datagen:``).

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
    values: list[Any] | None = None
    weights: list[float] | None = Field(
        default=None, description="Draw weights aligned with values"
    )
    templates: list[str] | None = None
    provider: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    format: str | None = None

    @model_validator(mode="after")
    def _check_weights(self) -> ColumnHints:
        """Weights only make sense alongside a matching values list."""
        if self.weights is not None:
            if self.values is None:
                raise ValueError("datagen hints: 'weights' requires 'values'")
            if len(self.weights) != len(self.values):
                raise ValueError(
                    f"datagen hints: {len(self.weights)} weights for {len(self.values)} values"
                )
        return self


class Scenario(BaseModel):
    """A coherent event shape drawn once per event (schema-level ``datagen.scenarios``).

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


class DatagenError(Exception):
    """A generation request that cannot be served (bad schema, bad hint, ...)."""
