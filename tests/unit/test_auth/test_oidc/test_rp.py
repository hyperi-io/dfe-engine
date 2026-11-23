#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/test_rp.py
#  Purpose:      Unit tests for OIDC relying-party claim extraction and userinfo filling
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Unit tests for OIDC relying-party claim extraction and userinfo filling."""

import ssl

import pytest

from dfe_engine.auth.oidc.models import OIDCProvider
from dfe_engine.auth.oidc.registry import OIDCProviderRegistry
from dfe_engine.auth.oidc.rp import (
    UserinfoSubjectMismatchError,
    build_relying_party,
    extract_identity,
    fill_from_userinfo_endpoint,
    merge_userinfo_claims,
    needs_directory_groups,
    needs_userinfo,
)
from dfe_engine.settings import load_settings
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


class TestNeedsUserinfo:
    @pytest.mark.parametrize(
        ("claims", "expected"),
        [
            ({"sub": "u1", "name": "Ada", "email": "ada@example.com"}, False),
            ({"sub": "u1", "preferred_username": "ada", "email": "ada@example.com"}, False),
            # The case the name-only trigger missed: the email fell back to the username.
            ({"sub": "u1", "name": "Ada", "preferred_username": "ada"}, True),
            ({"sub": "u1", "name": "Ada", "email": "  "}, True),
            ({"sub": "u1", "email": "ada@example.com"}, True),
            ({"sub": "u1"}, True),
        ],
        ids=["name-and-email", "username-and-email", "no-email", "blank-email", "no-name", "bare"],
    )
    def test_matches_expected(self, claims: dict, expected: bool):
        assert needs_userinfo(claims=claims) is expected

    @pytest.mark.parametrize(
        ("claims", "expected"),
        [
            ({"sub": "u1", "name": "Ada", "email": "ada@example.com"}, True),
            (
                {"sub": "u1", "name": "Ada", "email": "ada@example.com", "email_verified": False},
                False,
            ),
            ({"sub": "u1", "name": "Ada", "email": "ada@example.com", "xms_edov": True}, False),
        ],
        ids=["no-proof", "email-verified-false", "xms-edov"],
    )
    def test_a_trusted_provider_wants_email_proof(self, claims: dict, expected: bool):
        assert needs_userinfo(claims=claims, wants_email_proof=True) is expected


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


class TestProviderTls:
    """Every provider call goes out on the TLS profile the settings name."""

    @pytest.mark.parametrize(
        ("profile", "floor"),
        [
            (None, ssl.TLSVersion.TLSv1_2),
            ("prod", ssl.TLSVersion.TLSv1_2),
            ("highsec", ssl.TLSVersion.TLSv1_3),
        ],
    )
    def test_the_provider_client_takes_the_settings_profile(
        self, tmp_path, monkeypatch, profile, floor
    ):
        monkeypatch.delenv("DFE_ENGINE_CRYPTO_PROFILE", raising=False)
        if profile is not None:
            monkeypatch.setenv("DFE_ENGINE_CRYPTO_PROFILE", profile)
        settings = load_settings()
        registry = OIDCProviderRegistry(tmp_path / "oidc")
        registry.create("corp-sso", OIDCProvider(type="generic", issuer="https://sso.example.com"))

        rp = build_relying_party(registry, crypto_profile=settings.crypto_profile)

        assert rp is not None
        session = rp._client("corp-sso")._get_oauth_client()
        context = session._transport._pool._ssl_context
        assert (context.minimum_version, context.verify_mode) == (floor, ssl.CERT_REQUIRED)
