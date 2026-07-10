#  Project:      dfe-engine
#  File:         src/dfe_engine/transport_filter/validate.py
#  Purpose:      Validate filter rules against tier gate configuration
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Validate filter rules against tier gate configuration.

Mirrors the validation logic in
``scalo::transport::filter::TransportFilterEngine::new()`` —
called by the control plane before filter configs reach rustlib, so
operators get immediate feedback in the UI/CLI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from dfe_engine.cel import (
    FilterTier,
    classify_expression,
)
from dfe_engine.transport_filter.models import (
    FilterAction,
    FilterRule,
    TransportFilterTierConfig,
)

Direction = Literal["in", "out"]


@dataclass
class FilterValidationError:
    """A single filter validation error."""

    index: int
    """Index of the offending filter in the list."""

    expression: str
    """The expression that failed validation."""

    error: str
    """Human-readable error message."""

    tier: FilterTier | None = None
    """The tier the expression was classified as (if classification succeeded)."""

    def __str__(self) -> str:
        return f"filter_{self.index}: '{self.expression}' — {self.error}"


def validate_filter_rules(
    rules: list[FilterRule],
    direction: Direction,
    tier_config: TransportFilterTierConfig,
    has_dlq_configured: bool = True,
) -> list[FilterValidationError]:
    """Validate a list of filter rules against tier gates and DLQ availability.

    Returns a list of errors. An empty list means all rules are valid.

    Args:
        rules: Filter rules to validate.
        direction: "in" (inbound) or "out" (outbound) — determines which
            tier gate config applies.
        tier_config: Tier gate configuration (which tiers are enabled).
        has_dlq_configured: Whether the transport has a DLQ configured.
            If False, rules with `action: dlq` will fail validation.

    Examples:
        >>> from dfe_engine.transport_filter import FilterRule, FilterAction
        >>> rules = [FilterRule(expression="has(_table)", action=FilterAction.DROP)]
        >>> errors = validate_filter_rules(rules, "in", TransportFilterTierConfig())
        >>> len(errors)
        0
    """
    errors: list[FilterValidationError] = []

    for idx, rule in enumerate(rules):
        # Validate CEL syntax + tier
        try:
            classification = classify_expression(rule.expression)
        except ValueError as e:
            errors.append(
                FilterValidationError(
                    index=idx,
                    expression=rule.expression,
                    error=f"invalid expression: {e}",
                )
            )
            continue

        # Check tier gate
        if not _is_tier_allowed(classification.tier, direction, tier_config):
            enable_hint = _tier_enable_hint(classification.tier, direction)
            errors.append(
                FilterValidationError(
                    index=idx,
                    expression=rule.expression,
                    tier=classification.tier,
                    error=(
                        f"classified as {_tier_display(classification.tier)} but this tier "
                        f"is not enabled for {direction} filters. Set {enable_hint} to enable."
                    ),
                )
            )
            continue

        # Check DLQ availability
        if rule.action == FilterAction.DLQ and not has_dlq_configured:
            errors.append(
                FilterValidationError(
                    index=idx,
                    expression=rule.expression,
                    tier=classification.tier,
                    error=(
                        "has action=dlq but no DLQ is configured on this transport. "
                        "Either configure a DLQ topic or change the filter action to 'drop'."
                    ),
                )
            )
            continue

    return errors


def _is_tier_allowed(
    tier: FilterTier,
    direction: Direction,
    config: TransportFilterTierConfig,
) -> bool:
    """Check if a tier is allowed for a given direction."""
    if tier == FilterTier.TIER1:
        return True
    if tier == FilterTier.TIER2:
        if direction == "in":
            return config.allow_cel_filters_in or config.allow_complex_filters_in
        return config.allow_cel_filters_out or config.allow_complex_filters_out
    if tier == FilterTier.TIER3:
        if direction == "in":
            return config.allow_complex_filters_in
        return config.allow_complex_filters_out
    return False


def _tier_display(tier: FilterTier) -> str:
    """Human-readable tier name."""
    if tier == FilterTier.TIER1:
        return "Tier 1 (SIMD)"
    if tier == FilterTier.TIER2:
        return "Tier 2 (CEL)"
    if tier == FilterTier.TIER3:
        return "Tier 3 (complex CEL)"
    return "unknown"


def _tier_enable_hint(tier: FilterTier, direction: Direction) -> str:
    """Return the config key that needs to be enabled."""
    if tier == FilterTier.TIER2:
        return (
            "expression.allow_cel_filters_in: true"
            if direction == "in"
            else "expression.allow_cel_filters_out: true"
        )
    if tier == FilterTier.TIER3:
        return (
            "expression.allow_complex_filters_in: true"
            if direction == "in"
            else "expression.allow_complex_filters_out: true"
        )
    return ""


def warn_suboptimal_ordering(rules: list[FilterRule]) -> list[str]:
    """Warn if higher-tier filters precede lower-tier filters.

    Matches the ordering warning in rustlib's TransportFilterEngine::new().
    Returns a list of warning messages (empty if ordering is optimal).

    Example: `[Tier2, Tier1]` → warns because Tier 2 precedes Tier 1.
    Running Tier 1 first short-circuits before hitting the CEL engine.
    """
    warnings: list[str] = []
    tier_values = {FilterTier.TIER1: 1, FilterTier.TIER2: 2, FilterTier.TIER3: 3}

    # Classify all rules up-front
    classified: list[tuple[int, str, FilterTier]] = []
    for idx, rule in enumerate(rules):
        try:
            classification = classify_expression(rule.expression)
            classified.append((idx, rule.expression, classification.tier))
        except ValueError:
            continue

    # For each filter, check if any subsequent filter is in a LOWER tier
    # (which would be cheaper to evaluate first)
    for i, (idx, expr, tier) in enumerate(classified):
        my_val = tier_values.get(tier, 1)
        for _, _, later_tier in classified[i + 1 :]:
            later_val = tier_values.get(later_tier, 1)
            if later_val < my_val:
                warnings.append(
                    f"filter[{idx}] '{expr}' is {_tier_display(tier)} "
                    f"but precedes a lower-tier filter. Consider reordering — "
                    f"Tier 1 filters are faster and may short-circuit before "
                    f"the CEL engine runs."
                )
                break  # one warning per misplaced filter

    return warnings
