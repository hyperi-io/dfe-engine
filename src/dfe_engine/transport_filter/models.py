#  Project:      dfe-engine
#  File:         src/dfe_engine/transport_filter/models.py
#  Purpose:      Pydantic models for transport filter configuration
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Pydantic models mirroring the Rust transport filter config types.

These models serialise to exactly the same YAML/JSON that scalo-rs's
`TransportFilterEngine::new()` consumes.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class FilterAction(str, Enum):
    """Disposition action when a filter matches.

    Matches `scalo::transport::filter::FilterAction`.
    """

    DROP = "drop"
    """Silently discard the message (counted in metrics)."""

    DLQ = "dlq"
    """Route the message to the dead-letter queue (counted + security audit)."""


class FilterRule(BaseModel):
    """A single filter rule -- CEL expression + disposition action.

    Matches `scalo::transport::filter::FilterRule`.

    Filters use CEL syntax regardless of execution tier. The scalo-rs engine
    classifies expressions at config load time and selects the optimal
    execution strategy (Tier 1 SIMD, Tier 2 CEL, Tier 3 complex CEL).

    ## Example

    ```yaml
    - expression: 'has(_table)'
      action: drop
    - expression: 'status == "poison"'
      action: dlq
    ```
    """

    expression: str = Field(description="CEL expression to evaluate against each message payload")
    action: FilterAction = Field(
        default=FilterAction.DROP,
        description="Action to take when the expression matches",
    )


class TransportFilterTierConfig(BaseModel):
    """Tier gate configuration -- controls which filter tiers are enabled.

    Lives under the `expression` config cascade key. Tier 1 (SIMD field ops)
    is always enabled. Tier 2 (standard CEL) and Tier 3 (complex CEL with
    regex/iteration/time) require explicit opt-in.

    Matches `scalo::transport::filter::TransportFilterTierConfig`.
    """

    allow_cel_filters_in: bool = Field(
        default=False,
        description="Enable Tier 2 (CEL engine) for inbound transport filters",
    )
    allow_cel_filters_out: bool = Field(
        default=False,
        description="Enable Tier 2 (CEL engine) for outbound transport filters",
    )
    allow_complex_filters_in: bool = Field(
        default=False,
        description=(
            "Enable Tier 3 (complex CEL: regex, iteration, time) for inbound filters. "
            "Implies allow_cel_filters_in."
        ),
    )
    allow_complex_filters_out: bool = Field(
        default=False,
        description=(
            "Enable Tier 3 (complex CEL: regex, iteration, time) for outbound filters. "
            "Implies allow_cel_filters_out."
        ),
    )

    model_config = {"extra": "forbid"}
