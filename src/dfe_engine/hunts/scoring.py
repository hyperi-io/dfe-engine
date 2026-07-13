"""Hunt scoring — generic conditional score computation.

Scoring is opt-in. When a hunt YAML includes a ``scoring`` section,
factors are evaluated against matched data to adjust the base score.

Order of operations:
1. Start with base_score
2. Apply all matching ``add`` factors (additions first)
3. Apply all matching ``multiply`` factors
4. Clamp to 0–100

Condition expressions use **CEL (Common Expression Language)** via
``scalo.expression``. See ``docs/data-plane/expressions-cel.md`` for the
DFE expression profile.

Hunt YAML format::

    scoring:
      base_score: 75
      factors:
        - when: 'severity == "critical"'
          add: 20
        - when: "amount > 10000"
          multiply: 1.5

Or minimal::

    scoring:
      base_score: 75

Or omitted entirely (default score: 50, no factors).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator
from scalo.expression import (
    evaluate_condition,
)
from scalo.expression import (
    validate as validate_condition,
)

# ── Models ─────────────────────────────────────────────────────


class ScoreFactor(BaseModel):
    """A conditional scoring adjustment.

    Exactly one of ``add`` or ``multiply`` must be set.
    """

    when: str = Field(..., description="CEL condition expression (e.g. 'severity == \"critical\"')")
    add: int | None = Field(default=None, description="Points to add (can be negative)")
    multiply: float | None = Field(default=None, description="Multiplier to apply")

    @field_validator("when")
    @classmethod
    def _validate_when(cls, v: str) -> str:
        errors = validate_condition(v)
        if errors:
            raise ValueError(f"Invalid 'when' expression: {'; '.join(errors)}")
        return v

    @model_validator(mode="after")
    def _exactly_one_operation(self) -> ScoreFactor:
        if self.add is not None and self.multiply is not None:
            raise ValueError("ScoreFactor must have exactly one of 'add' or 'multiply', not both")
        if self.add is None and self.multiply is None:
            raise ValueError("ScoreFactor must have exactly one of 'add' or 'multiply'")
        return self


class ScoringConfig(BaseModel):
    """Scoring configuration for a hunt."""

    base_score: int = Field(
        default=50,
        ge=0,
        le=100,
        description="Base score before factor adjustments (0-100)",
    )
    factors: list[ScoreFactor] = Field(
        default_factory=list,
        description="Conditional adjustments evaluated against matched data",
    )


# ── Score Computation ─────────────────────────────────────────


def compute_score(
    config: ScoringConfig | None,
    data: dict[str, Any],
) -> int:
    """Compute the final score for a detection.

    Order of operations:
    1. Start with base_score
    2. Apply all matching ``add`` factors
    3. Apply all matching ``multiply`` factors
    4. Clamp to 0–100
    """
    if config is None:
        return 50

    score = float(config.base_score)

    # Phase 1: additions
    for factor in config.factors:
        if factor.add is not None and evaluate_condition(factor.when, data):
            score += factor.add

    # Phase 2: multiplications
    for factor in config.factors:
        if factor.multiply is not None and evaluate_condition(factor.when, data):
            score *= factor.multiply

    return max(0, min(100, int(round(score))))
