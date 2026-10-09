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

# The env var an UPDATE_PROVIDER_SECRET_SOURCE_CASES update names and the value the test puts in it.
SWITCHED_SECRET = "secret-from-env"
SWITCHED_SECRET_ENV = "DFE_TEST_SWITCHED_SECRET"

_ENTRA_GROUPS = {"mode": "api", "tenant_id": "contoso.onmicrosoft.com"}
_GOOGLE_GROUPS = {"admin_email": "admin@acme.com", "mode": "api"}
_OKTA_GROUPS = {"mode": "api", "okta_domain": "acme.okta.com"}
_ROTATED_SECRET = "rotated-secret"
_STORED_SECRET = "stored-secret"


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


class UpdateProviderSecretSourceCase(TypedDict):
    """One update to a provider holding a secret and where the provider reads that secret from afterwards."""

    id: str
    create_body: dict[str, Any]
    update_body: dict[str, Any]
    secret_field: str
    stored_path: str
    expected_source: dict[str, object]


UPDATE_PROVIDER_SECRET_SOURCE_CASES: list[UpdateProviderSecretSourceCase] = [
    {
        "id": "client_secret_switched_to_env",
        "create_body": {"client_secret": _STORED_SECRET},
        "update_body": {"client_secret_env": SWITCHED_SECRET_ENV},
        "secret_field": "client_secret",
        "stored_path": "oidc/sso/client_secret",
        "expected_source": {"path": "", "resolved": SWITCHED_SECRET, "stored": False},
    },
    {
        "id": "client_secret_switched_to_env_with_nothing_stored",
        "create_body": {},
        "update_body": {"client_secret_env": SWITCHED_SECRET_ENV},
        "secret_field": "client_secret",
        "stored_path": "oidc/sso/client_secret",
        "expected_source": {"path": "", "resolved": SWITCHED_SECRET, "stored": False},
    },
    {
        "id": "groups_api_token_switched_to_env",
        "create_body": {"groups": {**_OKTA_GROUPS, "api_token": _STORED_SECRET}, "type": "okta"},
        "update_body": {"groups": {**_OKTA_GROUPS, "api_token_env": SWITCHED_SECRET_ENV}},
        "secret_field": "groups.api_token",
        "stored_path": "oidc/sso/groups_api_token",
        "expected_source": {"path": "", "resolved": SWITCHED_SECRET, "stored": False},
    },
    {
        "id": "groups_client_secret_switched_to_env",
        "create_body": {
            "groups": {**_ENTRA_GROUPS, "client_secret": _STORED_SECRET},
            "type": "entra_id",
        },
        "update_body": {"groups": {**_ENTRA_GROUPS, "client_secret_env": SWITCHED_SECRET_ENV}},
        "secret_field": "groups.client_secret",
        "stored_path": "oidc/sso/groups_client_secret",
        "expected_source": {"path": "", "resolved": SWITCHED_SECRET, "stored": False},
    },
    {
        "id": "groups_service_account_json_switched_to_env",
        "create_body": {
            "groups": {**_GOOGLE_GROUPS, "service_account_json": _STORED_SECRET},
            "issuer": "https://accounts.google.com",
            "type": "google",
        },
        "update_body": {
            "groups": {**_GOOGLE_GROUPS, "service_account_json_env": SWITCHED_SECRET_ENV}
        },
        "secret_field": "groups.service_account_json",
        "stored_path": "oidc/sso/groups_service_account_json",
        "expected_source": {"path": "", "resolved": SWITCHED_SECRET, "stored": False},
    },
    {
        "id": "client_secret_blank_env_keeps_stored",
        "create_body": {"client_secret": _STORED_SECRET},
        "update_body": {"client_secret_env": ""},
        "secret_field": "client_secret",
        "stored_path": "oidc/sso/client_secret",
        "expected_source": {
            "path": "oidc/sso/client_secret",
            "resolved": _STORED_SECRET,
            "stored": True,
        },
    },
    {
        "id": "client_secret_omitted_keeps_stored",
        "create_body": {"client_secret": _STORED_SECRET},
        "update_body": {"display_name": "Renamed"},
        "secret_field": "client_secret",
        "stored_path": "oidc/sso/client_secret",
        "expected_source": {
            "path": "oidc/sso/client_secret",
            "resolved": _STORED_SECRET,
            "stored": True,
        },
    },
    {
        "id": "groups_api_token_blank_env_keeps_stored",
        "create_body": {"groups": {**_OKTA_GROUPS, "api_token": _STORED_SECRET}, "type": "okta"},
        "update_body": {"groups": {**_OKTA_GROUPS, "api_token_env": ""}},
        "secret_field": "groups.api_token",
        "stored_path": "oidc/sso/groups_api_token",
        "expected_source": {
            "path": "oidc/sso/groups_api_token",
            "resolved": _STORED_SECRET,
            "stored": True,
        },
    },
    {
        "id": "client_secret_value_rotates_stored",
        "create_body": {"client_secret": _STORED_SECRET},
        "update_body": {"client_secret": _ROTATED_SECRET, "client_secret_env": ""},
        "secret_field": "client_secret",
        "stored_path": "oidc/sso/client_secret",
        "expected_source": {
            "path": "oidc/sso/client_secret",
            "resolved": _ROTATED_SECRET,
            "stored": True,
        },
    },
    {
        "id": "client_secret_value_sent_with_env_is_stored",
        "create_body": {"client_secret": _STORED_SECRET},
        "update_body": {"client_secret": _ROTATED_SECRET, "client_secret_env": SWITCHED_SECRET_ENV},
        "secret_field": "client_secret",
        "stored_path": "oidc/sso/client_secret",
        "expected_source": {
            "path": "oidc/sso/client_secret",
            "resolved": _ROTATED_SECRET,
            "stored": True,
        },
    },
    {
        "id": "groups_api_token_value_rotates_stored",
        "create_body": {"groups": {**_OKTA_GROUPS, "api_token": _STORED_SECRET}, "type": "okta"},
        "update_body": {"groups": {**_OKTA_GROUPS, "api_token": _ROTATED_SECRET}},
        "secret_field": "groups.api_token",
        "stored_path": "oidc/sso/groups_api_token",
        "expected_source": {
            "path": "oidc/sso/groups_api_token",
            "resolved": _ROTATED_SECRET,
            "stored": True,
        },
    },
]
