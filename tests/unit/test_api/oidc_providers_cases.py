#  Project:      dfe-engine
#  File:         tests/unit/test_api/oidc_providers_cases.py
#  Purpose:      Case tables for the OIDC provider REST endpoint tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Case tables for the OIDC provider REST endpoint tests."""

from typing import Any, TypedDict

from dfe_engine.auth.oidc.models import OIDCProvider
from tests.unit.test_auth.factories import make_oidc_provider

# The env var naming the mock directory fixture a SYNC_PROVIDER_CASES provider in api mode reads.
SYNC_DIRECTORY_ENV = "DFE_TEST_SYNC_PROVIDER_DIRECTORY"


class SyncProviderCase(TypedDict):
    id: str
    provider: OIDCProvider
    expected_body: dict[str, Any]


SYNC_PROVIDER_CASES: list[SyncProviderCase] = [
    {
        "id": "token_claim_mode",
        "provider": make_oidc_provider(
            groups={"mode": "token_claim"}, issuer="https://example.okta.com", type="okta"
        ),
        "expected_body": {
            "created": 0,
            "error": None,
            "groups_skipped": 0,
            "skipped": "Groups mode is 'token_claim': groups come from each login's token, so "
            "there is no directory to sync; link a group to the IdP group by its source ID",
            "total": 0,
            "updated": 0,
        },
    },
    {
        "id": "disabled",
        "provider": make_oidc_provider(
            enabled=False,
            groups={"mode": "api", "okta_domain": "example.okta.com"},
            issuer="https://example.okta.com",
            type="okta",
        ),
        "expected_body": {
            "created": 0,
            "error": None,
            "groups_skipped": 0,
            "skipped": "The provider is disabled",
            "total": 0,
            "updated": 0,
        },
    },
    {
        "id": "api_mode_ran",
        "provider": make_oidc_provider(
            groups={
                "directory_backend": "mock",
                "mock_directory_env": SYNC_DIRECTORY_ENV,
                "mode": "api",
            }
        ),
        "expected_body": {
            "created": 1,
            "error": None,
            "groups_skipped": 0,
            "skipped": None,
            "total": 1,
            "updated": 0,
        },
    },
]
