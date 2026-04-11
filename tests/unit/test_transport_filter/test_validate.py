#  Project:      dfe-engine
#  File:         tests/unit/test_transport_filter/test_validate.py
#  Purpose:      Tests for transport filter validation
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for dfe_engine.transport_filter.validate."""

from __future__ import annotations

from dfe_engine.transport_filter import (
    FilterAction,
    FilterRule,
    TransportFilterTierConfig,
    validate_filter_rules,
)
from dfe_engine.transport_filter.validate import warn_suboptimal_ordering


class TestValidation:
    def test_tier1_rules_valid_by_default(self) -> None:
        rules = [
            FilterRule(expression="has(_table)", action=FilterAction.DROP),
            FilterRule(expression='status == "poison"', action=FilterAction.DLQ),
        ]
        errors = validate_filter_rules(rules, "in", TransportFilterTierConfig())
        assert errors == []

    def test_tier2_rejected_without_opt_in(self) -> None:
        rules = [
            FilterRule(
                expression='severity > 3 && source != "internal"',
                action=FilterAction.DROP,
            )
        ]
        errors = validate_filter_rules(rules, "in", TransportFilterTierConfig())
        assert len(errors) == 1
        assert "Tier 2" in errors[0].error
        assert "allow_cel_filters_in" in errors[0].error

    def test_tier2_accepted_with_opt_in(self) -> None:
        rules = [
            FilterRule(
                expression='severity > 3 && source != "internal"',
                action=FilterAction.DROP,
            )
        ]
        tier_config = TransportFilterTierConfig(allow_cel_filters_in=True)
        errors = validate_filter_rules(rules, "in", tier_config)
        assert errors == []

    def test_tier3_rejected_without_complex_opt_in(self) -> None:
        rules = [
            FilterRule(
                expression='tags.exists(t, t == "pii")',
                action=FilterAction.DLQ,
            )
        ]
        tier_config = TransportFilterTierConfig(allow_cel_filters_in=True)
        errors = validate_filter_rules(rules, "in", tier_config)
        assert len(errors) == 1
        assert "Tier 3" in errors[0].error

    def test_dlq_without_dlq_configured_fails(self) -> None:
        rules = [
            FilterRule(expression='status == "poison"', action=FilterAction.DLQ),
        ]
        errors = validate_filter_rules(
            rules,
            "in",
            TransportFilterTierConfig(),
            has_dlq_configured=False,
        )
        assert len(errors) == 1
        assert "no DLQ is configured" in errors[0].error

    def test_invalid_expression_fails(self) -> None:
        rules = [
            FilterRule(expression="", action=FilterAction.DROP),
        ]
        errors = validate_filter_rules(rules, "in", TransportFilterTierConfig())
        assert len(errors) == 1

    def test_direction_gate_independence(self) -> None:
        """Tier 2 enabled inbound should NOT enable it outbound."""
        rules = [
            FilterRule(
                expression="size(items) > 0",
                action=FilterAction.DROP,
            )
        ]
        tier_config = TransportFilterTierConfig(allow_cel_filters_in=True)
        # Inbound: allowed
        assert validate_filter_rules(rules, "in", tier_config) == []
        # Outbound: rejected
        errors = validate_filter_rules(rules, "out", tier_config)
        assert len(errors) == 1

    def test_multiple_errors_collected(self) -> None:
        rules = [
            FilterRule(expression="has(_table)", action=FilterAction.DROP),  # ok
            FilterRule(
                expression="size(items) > 0", action=FilterAction.DROP
            ),  # Tier 2 error
            FilterRule(
                expression='field.matches("^p.*")', action=FilterAction.DROP
            ),  # Tier 3 error
        ]
        errors = validate_filter_rules(rules, "in", TransportFilterTierConfig())
        assert len(errors) == 2
        assert errors[0].index == 1
        assert errors[1].index == 2


class TestOrderingWarnings:
    def test_optimal_order_no_warnings(self) -> None:
        rules = [
            FilterRule(expression="has(_internal)", action=FilterAction.DROP),
            FilterRule(expression='status == "poison"', action=FilterAction.DLQ),
        ]
        warnings = warn_suboptimal_ordering(rules)
        assert warnings == []

    def test_tier2_before_tier1_warns(self) -> None:
        rules = [
            FilterRule(expression="size(x) > 0", action=FilterAction.DROP),  # Tier 2
            FilterRule(expression="has(_table)", action=FilterAction.DROP),  # Tier 1
        ]
        warnings = warn_suboptimal_ordering(rules)
        assert len(warnings) == 1
        assert "Tier 2" in warnings[0]
