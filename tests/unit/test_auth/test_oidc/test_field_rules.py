#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_field_rules.py
#  Purpose:      Unit tests for the per-type OIDC provider field rules
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Unit tests for the per-type OIDC provider field rules."""

import pytest

from dfe_engine.auth.oidc.field_rules import field_problems
from tests.unit.test_auth.test_oidc.field_rules_cases import (
    FIELD_PROBLEMS_CASES,
    FieldProblemsCase,
)


class TestFieldProblems:
    @pytest.mark.parametrize(
        "case", FIELD_PROBLEMS_CASES, ids=[case["id"] for case in FIELD_PROBLEMS_CASES]
    )
    def test_matches_expected(self, case: FieldProblemsCase):
        problems = field_problems(
            provider=case["provider"], sent_group_fields=case["sent_group_fields"]
        )
        assert problems == case["expected_problems"]
