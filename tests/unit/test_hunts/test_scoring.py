"""Tests for hunt scoring — generic conditional score computation.

Uses CEL (Common Expression Language) via scalo.expression.
"""

from unittest.mock import patch

import pytest

from dfe_engine.hunts.scoring import (
    ScoreFactor,
    ScoringConfig,
    compute_score,
    evaluate_condition,
    validate_condition,
)

# ── ScoreFactor Validation ─────────────────────────────────────


class TestScoreFactor:
    def test_add_factor(self):
        f = ScoreFactor(when="severity == 'critical'", add=20)
        assert f.add == 20
        assert f.multiply is None

    def test_multiply_factor(self):
        f = ScoreFactor(when="amount > 10000", multiply=1.5)
        assert f.multiply == 1.5
        assert f.add is None

    def test_both_raises(self):
        with pytest.raises(ValueError, match="exactly one"):
            ScoreFactor(when="x == 1", add=10, multiply=1.5)

    def test_neither_raises(self):
        with pytest.raises(ValueError, match="exactly one"):
            ScoreFactor(when="x == 1")

    def test_negative_add(self):
        f = ScoreFactor(when="x == 1", add=-10)
        assert f.add == -10

    def test_zero_multiply(self):
        f = ScoreFactor(when="x == 1", multiply=0.0)
        assert f.multiply == 0.0


# ── ScoringConfig Validation ──────────────────────────────────


class TestScoringConfig:
    def test_defaults(self):
        cfg = ScoringConfig()
        assert cfg.base_score == 50
        assert cfg.factors == []

    def test_custom_base_score(self):
        cfg = ScoringConfig(base_score=75)
        assert cfg.base_score == 75

    def test_base_score_too_high(self):
        with pytest.raises(ValueError):
            ScoringConfig(base_score=101)

    def test_base_score_too_low(self):
        with pytest.raises(ValueError):
            ScoringConfig(base_score=-1)

    def test_base_score_zero(self):
        cfg = ScoringConfig(base_score=0)
        assert cfg.base_score == 0

    def test_base_score_100(self):
        cfg = ScoringConfig(base_score=100)
        assert cfg.base_score == 100

    def test_from_dict(self):
        """Simulate parsing from hunt YAML."""
        data = {
            "base_score": 75,
            "factors": [
                {"when": "severity == 'critical'", "add": 20},
                {"when": "amount > 10000", "multiply": 1.5},
            ],
        }
        cfg = ScoringConfig(**data)
        assert cfg.base_score == 75
        assert len(cfg.factors) == 2
        assert cfg.factors[0].add == 20
        assert cfg.factors[1].multiply == 1.5


# ── evaluate_condition ─────────────────────────────────────────


class TestEvaluateCondition:
    def test_string_equality(self):
        assert evaluate_condition("severity == 'critical'", {"severity": "critical"}) is True

    def test_string_equality_no_match(self):
        assert evaluate_condition("severity == 'critical'", {"severity": "low"}) is False

    def test_string_inequality(self):
        assert evaluate_condition("severity != 'low'", {"severity": "critical"}) is True

    def test_numeric_gt(self):
        assert evaluate_condition("amount > 10000", {"amount": 15000}) is True

    def test_numeric_gt_no_match(self):
        assert evaluate_condition("amount > 10000", {"amount": 5000}) is False

    def test_numeric_ge(self):
        assert evaluate_condition("count >= 100", {"count": 100}) is True

    def test_numeric_lt(self):
        assert evaluate_condition("risk < 5", {"risk": 3}) is True

    def test_numeric_le(self):
        assert evaluate_condition("risk <= 5", {"risk": 5}) is True

    def test_numeric_eq(self):
        assert evaluate_condition("port == 443", {"port": 443}) is True

    def test_explicit_type_cast(self):
        """Use int() for string-to-numeric conversion in CEL."""
        assert evaluate_condition("int(amount) > 10000", {"amount": "15000"}) is True

    def test_missing_field(self):
        assert evaluate_condition("severity == 'critical'", {}) is False

    def test_invalid_expression(self):
        assert evaluate_condition("== broken syntax", {"x": 1}) is False

    def test_empty_expression(self):
        assert evaluate_condition("", {"x": 1}) is False

    def test_in_operator(self):
        assert evaluate_condition('status in ["active", "pending"]', {"status": "active"}) is True

    def test_in_no_match(self):
        assert evaluate_condition('status in ["blocked", "banned"]', {"status": "active"}) is False

    def test_not_in_operator(self):
        assert (
            evaluate_condition('!(status in ["blocked", "banned"])', {"status": "active"}) is True
        )

    def test_double_quoted_string(self):
        assert evaluate_condition('severity == "critical"', {"severity": "critical"}) is True

    def test_nested_field_access(self):
        """CEL uses dot notation for nested field access."""
        assert evaluate_condition("event.type == 'login'", {"event": {"type": "login"}}) is True

    def test_boolean_true(self):
        assert evaluate_condition("enabled == true", {"enabled": True}) is True

    def test_boolean_false(self):
        assert evaluate_condition("enabled == false", {"enabled": False}) is True

    def test_float_comparison(self):
        assert evaluate_condition("score > 0.5", {"score": 0.8}) is True

    def test_in_numeric_list(self):
        assert evaluate_condition("port in [80, 443, 8080]", {"port": 443}) is True

    def test_compound_condition(self):
        assert (
            evaluate_condition(
                'severity == "critical" && amount > 10000',
                {"severity": "critical", "amount": 15000},
            )
            is True
        )

    def test_logical_or(self):
        assert (
            evaluate_condition(
                'severity == "critical" || amount > 50000',
                {"severity": "low", "amount": 100000},
            )
            is True
        )

    def test_logical_not(self):
        assert evaluate_condition("!is_test", {"is_test": False}) is True

    def test_string_contains(self):
        assert evaluate_condition('msg.contains("error")', {"msg": "an error occurred"}) is True

    def test_has_field(self):
        assert evaluate_condition("has(user.name)", {"user": {"name": "admin"}}) is True

    def test_has_missing_field(self):
        assert evaluate_condition("has(user.name)", {"user": {}}) is False


# ── validate_condition ─────────────────────────────────────────


class TestValidateCondition:
    def test_valid_expression(self):
        assert validate_condition("severity == 'critical'") == []

    def test_valid_numeric(self):
        assert validate_condition("amount > 10000") == []

    def test_valid_in(self):
        assert validate_condition('status in ["a", "b"]') == []

    def test_valid_not_in(self):
        assert validate_condition('!(status in ["x", "y"])') == []

    def test_valid_compound(self):
        assert validate_condition("a == 1 && b > 2") == []

    def test_valid_string_function(self):
        assert validate_condition('msg.contains("test")') == []

    def test_empty_returns_error(self):
        errors = validate_condition("")
        assert len(errors) == 1
        assert "empty" in errors[0].lower()

    def test_invalid_syntax_returns_error(self):
        errors = validate_condition("== broken")
        assert len(errors) == 1

    def test_missing_operator_returns_error(self):
        errors = validate_condition("severity critical")
        assert len(errors) == 1

    def test_disallowed_function_rejected(self):
        errors = validate_condition("[1,2].map(x, x * 2)")
        assert len(errors) == 1
        assert "map()" in errors[0]

    def test_score_factor_rejects_invalid_when(self):
        with pytest.raises(ValueError, match="Invalid 'when' expression"):
            ScoreFactor(when="== broken syntax", add=10)

    def test_score_factor_accepts_valid_when(self):
        f = ScoreFactor(when="severity == 'critical'", add=10)
        assert f.when == "severity == 'critical'"


# ── compute_score ──────────────────────────────────────────────


class TestComputeScore:
    def test_none_config_returns_default(self):
        assert compute_score(None, {}) == 50

    def test_base_only(self):
        cfg = ScoringConfig(base_score=75)
        assert compute_score(cfg, {}) == 75

    def test_no_factors(self):
        cfg = ScoringConfig(base_score=60, factors=[])
        assert compute_score(cfg, {}) == 60

    def test_single_add_match(self):
        cfg = ScoringConfig(
            base_score=50,
            factors=[ScoreFactor(when="severity == 'critical'", add=20)],
        )
        assert compute_score(cfg, {"severity": "critical"}) == 70

    def test_single_add_no_match(self):
        cfg = ScoringConfig(
            base_score=50,
            factors=[ScoreFactor(when="severity == 'critical'", add=20)],
        )
        assert compute_score(cfg, {"severity": "low"}) == 50

    def test_single_multiply_match(self):
        cfg = ScoringConfig(
            base_score=60,
            factors=[ScoreFactor(when="amount > 10000", multiply=1.5)],
        )
        assert compute_score(cfg, {"amount": 15000}) == 90

    def test_add_then_multiply_order(self):
        """Additions applied before multiplications."""
        cfg = ScoringConfig(
            base_score=50,
            factors=[
                ScoreFactor(when="x == 1", add=10),
                ScoreFactor(when="x == 1", multiply=2.0),
            ],
        )
        # (50 + 10) * 2.0 = 120 → clamped to 100
        assert compute_score(cfg, {"x": 1}) == 100

    def test_multiple_adds(self):
        cfg = ScoringConfig(
            base_score=50,
            factors=[
                ScoreFactor(when="a == 1", add=10),
                ScoreFactor(when="b == 2", add=15),
            ],
        )
        assert compute_score(cfg, {"a": 1, "b": 2}) == 75

    def test_clamp_above_100(self):
        cfg = ScoringConfig(
            base_score=90,
            factors=[ScoreFactor(when="x == 1", add=50)],
        )
        assert compute_score(cfg, {"x": 1}) == 100

    def test_clamp_below_0(self):
        cfg = ScoringConfig(
            base_score=10,
            factors=[ScoreFactor(when="x == 1", add=-50)],
        )
        assert compute_score(cfg, {"x": 1}) == 0

    def test_negative_add_subtraction(self):
        cfg = ScoringConfig(
            base_score=80,
            factors=[ScoreFactor(when="is_test == true", add=-30)],
        )
        assert compute_score(cfg, {"is_test": True}) == 50

    def test_multiply_by_zero(self):
        cfg = ScoringConfig(
            base_score=75,
            factors=[ScoreFactor(when="x == 1", multiply=0.0)],
        )
        assert compute_score(cfg, {"x": 1}) == 0

    def test_mixed_factors_partial_match(self):
        """Only matching factors applied."""
        cfg = ScoringConfig(
            base_score=50,
            factors=[
                ScoreFactor(when="severity == 'critical'", add=20),
                ScoreFactor(when="is_admin == true", multiply=2.0),
                ScoreFactor(when="region == 'eu'", add=10),
            ],
        )
        # severity matches (+20), is_admin doesn't match, region doesn't match
        assert compute_score(cfg, {"severity": "critical"}) == 70


# ── Alert Integration ──────────────────────────────────────────


class TestScoringAlertIntegration:
    def test_score_in_alert_template(self):
        """Score appears in alert body via template."""
        from dfe_engine.hunts.alert import AlertConfig, AlertDispatcher

        config = AlertConfig(
            channels=["slack://test"],
            triggers=[{"when": "result_count > 0"}],
            body_template="Hunt {hunt_name}: score={score}",
        )
        dispatcher = AlertDispatcher(config)

        with patch("apprise.Apprise.notify", return_value=True) as mock_notify:
            dispatcher.evaluate_and_send(
                hunt_name="test_hunt",
                customer="acme",
                rule_name="test_rule",
                result_count=5,
                score=85,
            )
        body = mock_notify.call_args.kwargs.get("body", mock_notify.call_args[1].get("body", ""))
        assert "score=85" in body

    def test_scoring_config_from_yaml(self):
        """ScoringConfig parsed from hunt YAML dict."""
        hunt_data = {
            "scoring": {
                "base_score": 75,
                "factors": [
                    {"when": "severity == 'critical'", "add": 20},
                ],
            }
        }
        scoring_data = hunt_data.get("scoring")
        cfg = ScoringConfig(**scoring_data)
        assert cfg.base_score == 75
        assert len(cfg.factors) == 1

    def test_no_scoring_section(self):
        hunt_data = {"name": "test_hunt", "cron": "*/5 * * * *"}
        assert hunt_data.get("scoring") is None
