#  Project:      dfe-engine
#  File:         tests/unit/test_git_identity.py
#  Purpose:      Tests for git author identity derivation
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from typing import TypedDict

import pytest
from dulwich.repo import check_user_identity

from dfe_engine.auth.models import AuthContext
from dfe_engine.git_identity import git_author


class GitAuthorCase(TypedDict):
    id: str
    user: AuthContext
    expected: str


GIT_AUTHOR_CASES: list[GitAuthorCase] = [
    {
        "id": "oidc_email",
        "user": AuthContext(user_id="alice", email="alice@corp.com"),
        "expected": "alice <alice@corp.com>",
    },
    {
        "id": "local_account_no_email",
        "user": AuthContext(user_id="admin"),
        "expected": "admin <admin@dfe.local>",
    },
    {
        "id": "api_key_colon_sanitised",
        "user": AuthContext(user_id="apikey:ci-bot"),
        "expected": "apikey:ci-bot <apikey-ci-bot@dfe.local>",
    },
    {
        "id": "dev_mode",
        "user": AuthContext(user_id="dev"),
        "expected": "dev <dev@dfe.local>",
    },
    {
        "id": "uppercase_local_part_lowered",
        "user": AuthContext(user_id="Operator"),
        "expected": "Operator <operator@dfe.local>",
    },
]


class TestGitAuthor:
    @pytest.mark.parametrize("case", GIT_AUTHOR_CASES, ids=[c["id"] for c in GIT_AUTHOR_CASES])
    def test_matches_expected(self, case: GitAuthorCase):
        assert git_author(case["user"]) == case["expected"]

    @pytest.mark.parametrize("case", GIT_AUTHOR_CASES, ids=[c["id"] for c in GIT_AUTHOR_CASES])
    def test_is_valid_git_identity(self, case: GitAuthorCase):
        # Every derived identity must pass dulwich's 'Name <email>' check --
        # the validation that rejected a bare 'admin' and triggered the 422.
        check_user_identity(git_author(case["user"]).encode())

    def test_custom_fallback_domain(self):
        user = AuthContext(user_id="admin")
        assert git_author(user, fallback_domain="acme.io") == "admin <admin@acme.io>"
