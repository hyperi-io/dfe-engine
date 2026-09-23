#  Project:      dfe-engine
#  File:         tests/unit/test_cel/test_syntax.py
#  Purpose:      Tests for one-shot CEL syntax check
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for dfe_engine.cel.syntax.check_syntax."""

from __future__ import annotations

from dfe_engine.cel import FilterTier
from dfe_engine.cel.syntax import check_syntax


class TestValidExpressions:
    def test_has_field_valid(self) -> None:
        r = check_syntax("has(_table)")
        assert r.valid
        assert r.errors == []
        assert r.tier == FilterTier.TIER1
        assert r.op_kind == "field_exists"
        assert r.op_field == "_table"
        assert r.description is not None
        assert "_table" in r.description

    def test_field_equals_valid(self) -> None:
        r = check_syntax('status == "poison"')
        assert r.valid
        assert r.tier == FilterTier.TIER1
        assert r.op_kind == "field_equals"
        assert r.op_field == "status"
        assert r.op_value == "poison"
        assert r.description is not None

    def test_tier2_valid_but_requires_opt_in(self) -> None:
        r = check_syntax('severity > 3 && source != "internal"')
        assert r.valid
        assert r.tier == FilterTier.TIER2
        assert r.opt_in_required is not None
        assert "allow_cel_filters" in r.opt_in_required

    def test_tier3_valid_but_requires_complex_opt_in(self) -> None:
        r = check_syntax('field.matches("^prod-.*")')
        assert r.valid
        assert r.tier == FilterTier.TIER3
        assert r.opt_in_required is not None
        assert "allow_complex_filters" in r.opt_in_required


class TestInvalidExpressions:
    def test_empty_expression(self) -> None:
        r = check_syntax("")
        assert not r.valid
        assert len(r.errors) > 0

    def test_whitespace_only(self) -> None:
        r = check_syntax("   ")
        assert not r.valid

    def test_invalid_syntax(self) -> None:
        r = check_syntax("this is not valid ((( CEL")
        assert not r.valid
        assert len(r.errors) > 0


class TestProfileMode:
    def test_profile_off_allows_tier3(self) -> None:
        """With check_profile=False, Tier 3 expressions are classified not rejected."""
        r = check_syntax('field.matches("^prod-.*")', check_profile=False)
        assert r.valid
        assert r.tier == FilterTier.TIER3

    def test_profile_on_rejects_iteration(self) -> None:
        """With check_profile=True, iteration functions are profile violations."""
        # the scalo profile allows matches() but bans map/filter/exists/all
        r = check_syntax('tags.exists(t, t == "pii")', check_profile=True)
        assert not r.valid
        assert len(r.errors) > 0


class TestHumanDescriptions:
    def test_has_description(self) -> None:
        r = check_syntax("has(_table)")
        assert r.description is not None
        assert "exists" in r.description

    def test_equals_description(self) -> None:
        r = check_syntax('status == "poison"')
        assert r.description is not None
        assert "poison" in r.description

    def test_starts_with_description(self) -> None:
        r = check_syntax('host.startsWith("prod-")')
        assert r.description is not None
        assert "starts with" in r.description
