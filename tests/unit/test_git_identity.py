#  Project:      dfe-engine
#  File:         tests/unit/test_git_identity.py
#  Purpose:      Tests for git author/committer identity derivation
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from typing import TypedDict

import pytest
from dulwich.repo import Repo, check_user_identity

from dfe_engine.auth.models import AuthContext
from dfe_engine.git_identity import COMMITTER_IDENTITY, commit_file, git_author


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


class TestCommitFile:
    def test_records_split_author_and_committer(self, tmp_path):
        from hyperi_pylib.config import DirectoryConfigStore

        Repo.init(str(tmp_path))
        store = DirectoryConfigStore(directory=tmp_path, writable=True)
        store.start()
        try:
            target = tmp_path / "thing.yaml"
            target.write_text("name: thing\n")
            commit_file(store, target, "test: add thing", author="admin <admin@dfe.local>")
            repo = Repo(str(tmp_path))
            head = repo[repo.head()]
            repo.close()
            assert head.author == b"admin <admin@dfe.local>"
            assert head.committer == COMMITTER_IDENTITY.encode("utf-8")
            assert head.message == b"test: add thing"
        finally:
            store.stop()

    def test_non_git_store_is_noop(self, tmp_path):
        from hyperi_pylib.config import DirectoryConfigStore

        store = DirectoryConfigStore(directory=tmp_path, writable=True)
        store.start()
        try:
            target = tmp_path / "thing.yaml"
            target.write_text("name: thing\n")
            # No repo -- must not raise.
            commit_file(store, target, "test: add thing", author="admin <admin@dfe.local>")
        finally:
            store.stop()
