#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_deps.py
#  Purpose:      Unit tests for the api.deps session lookups
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from pathlib import Path

import pytest

from dfe_engine.api.deps import account_for_session_subject
from tests.unit.test_api.deps_cases import (
    ACCOUNT_FOR_SESSION_SUBJECT_CASES,
    AccountForSessionSubjectCase,
)
from tests.unit.test_auth.factories import make_account_store


class TestAccountForSessionSubject:
    @pytest.mark.parametrize(
        "case",
        ACCOUNT_FOR_SESSION_SUBJECT_CASES,
        ids=[case["id"] for case in ACCOUNT_FOR_SESSION_SUBJECT_CASES],
    )
    def test_matches_expected(self, tmp_path: Path, case: AccountForSessionSubjectCase):
        store = make_account_store(directory=tmp_path)
        for account in case["stored"]:
            store.put(account=account)
        bound = account_for_session_subject(store=store, user_id=case["subject"])
        assert getattr(bound, "username", None) == case["expected_username"]
