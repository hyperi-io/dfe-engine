#  Project:      dfe-engine
#  File:         tests/unit/test_api/oidc_providers_cases.py
#  Purpose:      Case tables for the OIDC provider REST endpoint tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Case tables for the OIDC provider REST endpoint tests."""

from typing import Any, TypedDict

# The env var an UPDATE_PROVIDER_SECRET_SOURCE_CASES update names and the value the test puts in it.
SWITCHED_SECRET = "secret-from-env"
SWITCHED_SECRET_ENV = "DFE_TEST_SWITCHED_SECRET"
# An env var the tests name and never set, so a move onto it is refused.
UNSET_SECRET_ENV = "DFE_TEST_STORED_SECRET_ENV"

_ENTRA = {"type": "entra_id"}
_ENTRA_GROUPS = {"mode": "api", "tenant_id": "contoso.onmicrosoft.com"}
_GOOGLE = {"issuer": "https://accounts.google.com", "type": "google"}
_GOOGLE_GROUPS = {"mode": "api"}
_OKTA = {"type": "okta"}
_OKTA_GROUPS = {"mode": "api", "okta_domain": "acme.okta.com"}
_ROTATED_SECRET = "rotated-secret"
_STORED_SECRET = "stored-secret"


class CreateProviderUnsetEnvCase(TypedDict):
    """One create naming an env var the engine does not set for a secret it sends no value for."""

    id: str
    create_body: dict[str, Any]
    expected_fields: list[str]


CREATE_PROVIDER_UNSET_ENV_CASES: list[CreateProviderUnsetEnvCase] = [
    {
        "id": "client_secret",
        "create_body": {"client_secret_env": UNSET_SECRET_ENV},
        "expected_fields": ["client_secret_env"],
    },
    {
        "id": "groups_api_token",
        "create_body": {**_OKTA, "groups": {**_OKTA_GROUPS, "api_token_env": UNSET_SECRET_ENV}},
        "expected_fields": ["groups.api_token_env"],
    },
    {
        "id": "groups_client_secret",
        "create_body": {
            **_ENTRA,
            "groups": {**_ENTRA_GROUPS, "client_secret_env": UNSET_SECRET_ENV},
        },
        "expected_fields": ["groups.client_secret_env"],
    },
    {
        "id": "groups_service_account_json",
        "create_body": {
            **_GOOGLE,
            "groups": {**_GOOGLE_GROUPS, "service_account_json_env": UNSET_SECRET_ENV},
        },
        "expected_fields": ["groups.service_account_json_env"],
    },
    {
        "id": "every_secret_at_once",
        "create_body": {
            **_ENTRA,
            "client_secret_env": UNSET_SECRET_ENV,
            "groups": {**_ENTRA_GROUPS, "client_secret_env": UNSET_SECRET_ENV},
        },
        "expected_fields": ["client_secret_env", "groups.client_secret_env"],
    },
]


class UpdateTenantIdEnvCase(TypedDict):
    """One groups block sent to an Entra provider naming ENTRA_TENANT_ID plus the tenant id env var it keeps."""

    id: str
    update_groups: dict[str, Any]
    expected_tenant_id_env: str


UPDATE_TENANT_ID_ENV_CASES: list[UpdateTenantIdEnvCase] = [
    {
        "id": "omitted_keeps_the_name",
        "update_groups": {"mode": "api", "sync_interval": 900},
        "expected_tenant_id_env": "ENTRA_TENANT_ID",
    },
    {
        "id": "blank_clears_the_name",
        "update_groups": {
            "mode": "api",
            "tenant_id": "contoso.onmicrosoft.com",
            "tenant_id_env": "",
        },
        "expected_tenant_id_env": "",
    },
    {
        "id": "a_new_name_replaces_it",
        "update_groups": {"mode": "api", "tenant_id_env": "ENTRA_TENANT"},
        "expected_tenant_id_env": "ENTRA_TENANT",
    },
]


class UpdateProviderSecretSourceCase(TypedDict):
    """One update to a provider holding a secret, its answer and where the provider reads that secret from afterwards."""

    id: str
    create_body: dict[str, Any]
    update_body: dict[str, Any]
    secret_field: str
    stored_path: str
    expected_status: int
    expected_source: dict[str, object]


UPDATE_PROVIDER_SECRET_SOURCE_CASES: list[UpdateProviderSecretSourceCase] = [
    {
        "id": "client_secret_switched_to_env",
        "create_body": {"client_secret": _STORED_SECRET},
        "update_body": {"client_secret_env": SWITCHED_SECRET_ENV},
        "secret_field": "client_secret",
        "stored_path": "oidc/sso/client_secret",
        "expected_status": 200,
        "expected_source": {
            "env": SWITCHED_SECRET_ENV,
            "path": "",
            "resolved": SWITCHED_SECRET,
            "stored": False,
        },
    },
    {
        "id": "client_secret_switched_to_env_with_nothing_stored",
        "create_body": {},
        "update_body": {"client_secret_env": SWITCHED_SECRET_ENV},
        "secret_field": "client_secret",
        "stored_path": "oidc/sso/client_secret",
        "expected_status": 200,
        "expected_source": {
            "env": SWITCHED_SECRET_ENV,
            "path": "",
            "resolved": SWITCHED_SECRET,
            "stored": False,
        },
    },
    {
        "id": "client_secret_switched_from_another_env",
        "create_body": {"client_secret": _STORED_SECRET, "client_secret_env": UNSET_SECRET_ENV},
        "update_body": {"client_secret_env": SWITCHED_SECRET_ENV},
        "secret_field": "client_secret",
        "stored_path": "oidc/sso/client_secret",
        "expected_status": 200,
        "expected_source": {
            "env": SWITCHED_SECRET_ENV,
            "path": "",
            "resolved": SWITCHED_SECRET,
            "stored": False,
        },
    },
    {
        "id": "groups_api_token_switched_to_env",
        "create_body": {**_OKTA, "groups": {**_OKTA_GROUPS, "api_token": _STORED_SECRET}},
        "update_body": {"groups": {**_OKTA_GROUPS, "api_token_env": SWITCHED_SECRET_ENV}},
        "secret_field": "groups.api_token",
        "stored_path": "oidc/sso/groups_api_token",
        "expected_status": 200,
        "expected_source": {
            "env": SWITCHED_SECRET_ENV,
            "path": "",
            "resolved": SWITCHED_SECRET,
            "stored": False,
        },
    },
    {
        "id": "groups_client_secret_switched_to_env",
        "create_body": {**_ENTRA, "groups": {**_ENTRA_GROUPS, "client_secret": _STORED_SECRET}},
        "update_body": {"groups": {**_ENTRA_GROUPS, "client_secret_env": SWITCHED_SECRET_ENV}},
        "secret_field": "groups.client_secret",
        "stored_path": "oidc/sso/groups_client_secret",
        "expected_status": 200,
        "expected_source": {
            "env": SWITCHED_SECRET_ENV,
            "path": "",
            "resolved": SWITCHED_SECRET,
            "stored": False,
        },
    },
    {
        "id": "groups_service_account_json_switched_to_env",
        "create_body": {
            **_GOOGLE,
            "groups": {**_GOOGLE_GROUPS, "service_account_json": _STORED_SECRET},
        },
        "update_body": {
            "groups": {**_GOOGLE_GROUPS, "service_account_json_env": SWITCHED_SECRET_ENV}
        },
        "secret_field": "groups.service_account_json",
        "stored_path": "oidc/sso/groups_service_account_json",
        "expected_status": 200,
        "expected_source": {
            "env": SWITCHED_SECRET_ENV,
            "path": "",
            "resolved": SWITCHED_SECRET,
            "stored": False,
        },
    },
    {
        "id": "client_secret_resent_env_drops_stored",
        "create_body": {"client_secret": _STORED_SECRET, "client_secret_env": SWITCHED_SECRET_ENV},
        "update_body": {"client_secret_env": SWITCHED_SECRET_ENV, "display_name": "Renamed"},
        "secret_field": "client_secret",
        "stored_path": "oidc/sso/client_secret",
        "expected_status": 200,
        "expected_source": {
            "env": SWITCHED_SECRET_ENV,
            "path": "",
            "resolved": SWITCHED_SECRET,
            "stored": False,
        },
    },
    {
        "id": "groups_api_token_resent_env_drops_stored",
        "create_body": {
            **_OKTA,
            "groups": {
                **_OKTA_GROUPS,
                "api_token": _STORED_SECRET,
                "api_token_env": SWITCHED_SECRET_ENV,
            },
        },
        "update_body": {"groups": {**_OKTA_GROUPS, "api_token_env": SWITCHED_SECRET_ENV}},
        "secret_field": "groups.api_token",
        "stored_path": "oidc/sso/groups_api_token",
        "expected_status": 200,
        "expected_source": {
            "env": SWITCHED_SECRET_ENV,
            "path": "",
            "resolved": SWITCHED_SECRET,
            "stored": False,
        },
    },
    {
        "id": "groups_client_secret_resent_env_drops_stored",
        "create_body": {
            **_ENTRA,
            "groups": {
                **_ENTRA_GROUPS,
                "client_secret": _STORED_SECRET,
                "client_secret_env": SWITCHED_SECRET_ENV,
            },
        },
        "update_body": {"groups": {**_ENTRA_GROUPS, "client_secret_env": SWITCHED_SECRET_ENV}},
        "secret_field": "groups.client_secret",
        "stored_path": "oidc/sso/groups_client_secret",
        "expected_status": 200,
        "expected_source": {
            "env": SWITCHED_SECRET_ENV,
            "path": "",
            "resolved": SWITCHED_SECRET,
            "stored": False,
        },
    },
    {
        "id": "groups_service_account_json_resent_env_drops_stored",
        "create_body": {
            **_GOOGLE,
            "groups": {
                **_GOOGLE_GROUPS,
                "service_account_json": _STORED_SECRET,
                "service_account_json_env": SWITCHED_SECRET_ENV,
            },
        },
        "update_body": {
            "groups": {**_GOOGLE_GROUPS, "service_account_json_env": SWITCHED_SECRET_ENV}
        },
        "secret_field": "groups.service_account_json",
        "stored_path": "oidc/sso/groups_service_account_json",
        "expected_status": 200,
        "expected_source": {
            "env": SWITCHED_SECRET_ENV,
            "path": "",
            "resolved": SWITCHED_SECRET,
            "stored": False,
        },
    },
    {
        "id": "client_secret_unset_env_keeps_stored",
        "create_body": {"client_secret": _STORED_SECRET},
        "update_body": {"client_secret_env": UNSET_SECRET_ENV},
        "secret_field": "client_secret",
        "stored_path": "oidc/sso/client_secret",
        "expected_status": 422,
        "expected_source": {
            "env": "",
            "path": "oidc/sso/client_secret",
            "resolved": _STORED_SECRET,
            "stored": True,
        },
    },
    {
        "id": "client_secret_resent_unset_env_keeps_stored",
        "create_body": {"client_secret": _STORED_SECRET, "client_secret_env": UNSET_SECRET_ENV},
        "update_body": {"client_secret_env": UNSET_SECRET_ENV, "display_name": "Renamed"},
        "secret_field": "client_secret",
        "stored_path": "oidc/sso/client_secret",
        "expected_status": 422,
        "expected_source": {
            "env": UNSET_SECRET_ENV,
            "path": "oidc/sso/client_secret",
            "resolved": _STORED_SECRET,
            "stored": True,
        },
    },
    {
        "id": "groups_api_token_unset_env_keeps_stored",
        "create_body": {**_OKTA, "groups": {**_OKTA_GROUPS, "api_token": _STORED_SECRET}},
        "update_body": {"groups": {**_OKTA_GROUPS, "api_token_env": UNSET_SECRET_ENV}},
        "secret_field": "groups.api_token",
        "stored_path": "oidc/sso/groups_api_token",
        "expected_status": 422,
        "expected_source": {
            "env": "",
            "path": "oidc/sso/groups_api_token",
            "resolved": _STORED_SECRET,
            "stored": True,
        },
    },
    {
        "id": "groups_client_secret_unset_env_keeps_stored",
        "create_body": {**_ENTRA, "groups": {**_ENTRA_GROUPS, "client_secret": _STORED_SECRET}},
        "update_body": {"groups": {**_ENTRA_GROUPS, "client_secret_env": UNSET_SECRET_ENV}},
        "secret_field": "groups.client_secret",
        "stored_path": "oidc/sso/groups_client_secret",
        "expected_status": 422,
        "expected_source": {
            "env": "",
            "path": "oidc/sso/groups_client_secret",
            "resolved": _STORED_SECRET,
            "stored": True,
        },
    },
    {
        "id": "groups_service_account_json_unset_env_keeps_stored",
        "create_body": {
            **_GOOGLE,
            "groups": {**_GOOGLE_GROUPS, "service_account_json": _STORED_SECRET},
        },
        "update_body": {"groups": {**_GOOGLE_GROUPS, "service_account_json_env": UNSET_SECRET_ENV}},
        "secret_field": "groups.service_account_json",
        "stored_path": "oidc/sso/groups_service_account_json",
        "expected_status": 422,
        "expected_source": {
            "env": "",
            "path": "oidc/sso/groups_service_account_json",
            "resolved": _STORED_SECRET,
            "stored": True,
        },
    },
    {
        "id": "client_secret_blank_env_keeps_stored",
        "create_body": {"client_secret": _STORED_SECRET, "client_secret_env": SWITCHED_SECRET_ENV},
        "update_body": {"client_secret_env": ""},
        "secret_field": "client_secret",
        "stored_path": "oidc/sso/client_secret",
        "expected_status": 200,
        "expected_source": {
            "env": "",
            "path": "oidc/sso/client_secret",
            "resolved": _STORED_SECRET,
            "stored": True,
        },
    },
    {
        "id": "client_secret_omitted_keeps_name_and_stored",
        "create_body": {"client_secret": _STORED_SECRET, "client_secret_env": SWITCHED_SECRET_ENV},
        "update_body": {"display_name": "Renamed"},
        "secret_field": "client_secret",
        "stored_path": "oidc/sso/client_secret",
        "expected_status": 200,
        "expected_source": {
            "env": SWITCHED_SECRET_ENV,
            "path": "oidc/sso/client_secret",
            "resolved": _STORED_SECRET,
            "stored": True,
        },
    },
    {
        "id": "groups_api_token_omitted_keeps_name_and_stored",
        "create_body": {
            **_OKTA,
            "groups": {
                **_OKTA_GROUPS,
                "api_token": _STORED_SECRET,
                "api_token_env": SWITCHED_SECRET_ENV,
            },
        },
        "update_body": {"groups": {**_OKTA_GROUPS, "sync_interval": 900}},
        "secret_field": "groups.api_token",
        "stored_path": "oidc/sso/groups_api_token",
        "expected_status": 200,
        "expected_source": {
            "env": SWITCHED_SECRET_ENV,
            "path": "oidc/sso/groups_api_token",
            "resolved": _STORED_SECRET,
            "stored": True,
        },
    },
    {
        "id": "groups_client_secret_omitted_keeps_name_and_stored",
        "create_body": {
            **_ENTRA,
            "groups": {
                **_ENTRA_GROUPS,
                "client_secret": _STORED_SECRET,
                "client_secret_env": SWITCHED_SECRET_ENV,
            },
        },
        "update_body": {"groups": {**_ENTRA_GROUPS, "sync_interval": 900}},
        "secret_field": "groups.client_secret",
        "stored_path": "oidc/sso/groups_client_secret",
        "expected_status": 200,
        "expected_source": {
            "env": SWITCHED_SECRET_ENV,
            "path": "oidc/sso/groups_client_secret",
            "resolved": _STORED_SECRET,
            "stored": True,
        },
    },
    {
        "id": "groups_service_account_json_omitted_keeps_name_and_stored",
        "create_body": {
            **_GOOGLE,
            "groups": {
                **_GOOGLE_GROUPS,
                "service_account_json": _STORED_SECRET,
                "service_account_json_env": SWITCHED_SECRET_ENV,
            },
        },
        "update_body": {"groups": {**_GOOGLE_GROUPS, "sync_interval": 900}},
        "secret_field": "groups.service_account_json",
        "stored_path": "oidc/sso/groups_service_account_json",
        "expected_status": 200,
        "expected_source": {
            "env": SWITCHED_SECRET_ENV,
            "path": "oidc/sso/groups_service_account_json",
            "resolved": _STORED_SECRET,
            "stored": True,
        },
    },
    {
        "id": "groups_api_token_blank_env_clears_name_keeps_stored",
        "create_body": {
            **_OKTA,
            "groups": {
                **_OKTA_GROUPS,
                "api_token": _STORED_SECRET,
                "api_token_env": SWITCHED_SECRET_ENV,
            },
        },
        "update_body": {"groups": {**_OKTA_GROUPS, "api_token_env": ""}},
        "secret_field": "groups.api_token",
        "stored_path": "oidc/sso/groups_api_token",
        "expected_status": 200,
        "expected_source": {
            "env": "",
            "path": "oidc/sso/groups_api_token",
            "resolved": _STORED_SECRET,
            "stored": True,
        },
    },
    {
        "id": "groups_client_secret_blank_env_clears_name_keeps_stored",
        "create_body": {
            **_ENTRA,
            "groups": {
                **_ENTRA_GROUPS,
                "client_secret": _STORED_SECRET,
                "client_secret_env": SWITCHED_SECRET_ENV,
            },
        },
        "update_body": {"groups": {**_ENTRA_GROUPS, "client_secret_env": ""}},
        "secret_field": "groups.client_secret",
        "stored_path": "oidc/sso/groups_client_secret",
        "expected_status": 200,
        "expected_source": {
            "env": "",
            "path": "oidc/sso/groups_client_secret",
            "resolved": _STORED_SECRET,
            "stored": True,
        },
    },
    {
        "id": "groups_service_account_json_blank_env_clears_name_keeps_stored",
        "create_body": {
            **_GOOGLE,
            "groups": {
                **_GOOGLE_GROUPS,
                "service_account_json": _STORED_SECRET,
                "service_account_json_env": SWITCHED_SECRET_ENV,
            },
        },
        "update_body": {"groups": {**_GOOGLE_GROUPS, "service_account_json_env": ""}},
        "secret_field": "groups.service_account_json",
        "stored_path": "oidc/sso/groups_service_account_json",
        "expected_status": 200,
        "expected_source": {
            "env": "",
            "path": "oidc/sso/groups_service_account_json",
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
        "expected_status": 200,
        "expected_source": {
            "env": "",
            "path": "oidc/sso/client_secret",
            "resolved": _ROTATED_SECRET,
            "stored": True,
        },
    },
    {
        "id": "client_secret_value_sent_with_an_unset_env_is_stored",
        "create_body": {"client_secret": _STORED_SECRET},
        "update_body": {"client_secret": _ROTATED_SECRET, "client_secret_env": UNSET_SECRET_ENV},
        "secret_field": "client_secret",
        "stored_path": "oidc/sso/client_secret",
        "expected_status": 200,
        "expected_source": {
            "env": UNSET_SECRET_ENV,
            "path": "oidc/sso/client_secret",
            "resolved": _ROTATED_SECRET,
            "stored": True,
        },
    },
    {
        "id": "groups_api_token_value_rotates_stored",
        "create_body": {**_OKTA, "groups": {**_OKTA_GROUPS, "api_token": _STORED_SECRET}},
        "update_body": {"groups": {**_OKTA_GROUPS, "api_token": _ROTATED_SECRET}},
        "secret_field": "groups.api_token",
        "stored_path": "oidc/sso/groups_api_token",
        "expected_status": 200,
        "expected_source": {
            "env": "",
            "path": "oidc/sso/groups_api_token",
            "resolved": _ROTATED_SECRET,
            "stored": True,
        },
    },
]
