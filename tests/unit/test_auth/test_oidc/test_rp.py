#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_rp.py
#  Purpose:      Unit tests for OIDC relying-party claim extraction and userinfo filling
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Unit tests for OIDC relying-party claim extraction and userinfo filling."""

from __future__ import annotations

import pytest

from dfe_engine.auth.oidc.rp import (
    UserinfoSubjectMismatchError,
    extract_identity,
    fill_from_userinfo_endpoint,
    merge_userinfo_claims,
    needs_directory_groups,
)
from tests.unit.test_auth.factories import make_oauth_client
from tests.unit.test_auth.test_oidc.local_idp import LocalIdp
from tests.unit.test_auth.test_oidc.rp_cases import (
    EXTRACT_IDENTITY_CASES,
    FILL_FROM_USERINFO_ENDPOINT_CASES,
    MERGE_USERINFO_CLAIMS_CASES,
    MERGE_USERINFO_CLAIMS_SUBJECT_MISMATCH_CASES,
    NEEDS_DIRECTORY_GROUPS_CASES,
    ExtractIdentityCase,
    FillFromUserinfoEndpointCase,
    MergeUserinfoClaimsCase,
    MergeUserinfoClaimsSubjectMismatchCase,
    NeedsDirectoryGroupsCase,
)


class TestExtractIdentity:
    @pytest.mark.parametrize(
        "case", EXTRACT_IDENTITY_CASES, ids=[case["id"] for case in EXTRACT_IDENTITY_CASES]
    )
    def test_matches_expected(self, case: ExtractIdentityCase):
        identity = extract_identity(provider=case["provider"], userinfo_claims=case["claims"])
        assert identity == case["expected_identity"]


class TestNeedsDirectoryGroups:
    @pytest.mark.parametrize(
        "case",
        NEEDS_DIRECTORY_GROUPS_CASES,
        ids=[case["id"] for case in NEEDS_DIRECTORY_GROUPS_CASES],
    )
    def test_matches_expected(self, case: NeedsDirectoryGroupsCase):
        needs = needs_directory_groups(identity=case["identity"], provider=case["provider"])
        assert needs == case["expected_needs"]


class TestMergeUserinfoClaims:
    @pytest.mark.parametrize(
        "case",
        MERGE_USERINFO_CLAIMS_CASES,
        ids=[case["id"] for case in MERGE_USERINFO_CLAIMS_CASES],
    )
    def test_matches_expected(self, case: MergeUserinfoClaimsCase):
        merged = merge_userinfo_claims(
            id_token_claims=case["id_token_claims"], userinfo_claims=case["userinfo_claims"]
        )
        assert merged == case["expected_claims"]

    @pytest.mark.parametrize(
        "case",
        MERGE_USERINFO_CLAIMS_SUBJECT_MISMATCH_CASES,
        ids=[case["id"] for case in MERGE_USERINFO_CLAIMS_SUBJECT_MISMATCH_CASES],
    )
    def test_subject_mismatch(self, case: MergeUserinfoClaimsSubjectMismatchCase):
        message = "userinfo response subject does not match the ID token"
        with pytest.raises(UserinfoSubjectMismatchError, match=message):
            merge_userinfo_claims(
                id_token_claims=case["id_token_claims"], userinfo_claims=case["userinfo_claims"]
            )


class TestFillFromUserinfoEndpoint:
    @pytest.mark.parametrize(
        "case",
        FILL_FROM_USERINFO_ENDPOINT_CASES,
        ids=[case["id"] for case in FILL_FROM_USERINFO_ENDPOINT_CASES],
    )
    async def test_matches_expected(self, local_idp: LocalIdp, case: FillFromUserinfoEndpointCase):
        local_idp.advertise_userinfo = case["advertise_userinfo"]
        local_idp.userinfo_body = case["userinfo_body"]
        local_idp.userinfo_status = case["userinfo_status"]
        claims = await fill_from_userinfo_endpoint(
            claims=case["id_token_claims"],
            client=make_oauth_client(server_metadata_url=local_idp.metadata_url),
            issuer=local_idp.base_url,
            token=case["token"],
        )
        assert claims == case["expected_claims"]
