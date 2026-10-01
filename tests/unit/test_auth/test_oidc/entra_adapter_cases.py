#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/entra_adapter_cases.py
#  Purpose:      Case tables for the Entra ID adapter's Graph credential resolution
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Case tables for the Entra ID adapter's Graph credential resolution."""

from typing import TypedDict

from dfe_engine.auth.oidc.adapters.entra import GraphCredentials
from dfe_engine.auth.oidc.models import OIDCProvider
from tests.unit.test_auth.factories import make_graph_credentials, make_oidc_provider

ENTRA_ISSUER = "https://login.microsoftonline.com/tid/v2.0"


class GraphCredentialsCase(TypedDict):
    id: str
    env: dict[str, str]
    expected_credentials: GraphCredentials
    provider: OIDCProvider


GRAPH_CREDENTIALS_CASES: list[GraphCredentialsCase] = [
    {
        "id": "tenant_value_wins_over_its_env_var",
        "env": {
            "DFE_TEST_ENTRA_SECRET": "group-secret",
            "DFE_TEST_ENTRA_TENANT": "tenant-from-env",
        },
        "expected_credentials": make_graph_credentials(
            client_id="cid", client_secret="group-secret", tenant_id="tenant-value"
        ),
        "provider": make_oidc_provider(
            client_id="cid",
            groups={
                "client_secret_env": "DFE_TEST_ENTRA_SECRET",
                "mode": "api",
                "tenant_id": "tenant-value",
                "tenant_id_env": "DFE_TEST_ENTRA_TENANT",
            },
            issuer=ENTRA_ISSUER,
            type="entra_id",
        ),
    },
    {
        "id": "tenant_from_its_env_var_without_a_value",
        "env": {
            "DFE_TEST_ENTRA_SECRET": "group-secret",
            "DFE_TEST_ENTRA_TENANT": "tenant-from-env",
        },
        "expected_credentials": make_graph_credentials(
            client_id="cid", client_secret="group-secret", tenant_id="tenant-from-env"
        ),
        "provider": make_oidc_provider(
            client_id="cid",
            groups={
                "client_secret_env": "DFE_TEST_ENTRA_SECRET",
                "mode": "api",
                "tenant_id_env": "DFE_TEST_ENTRA_TENANT",
            },
            issuer=ENTRA_ISSUER,
            type="entra_id",
        ),
    },
    {
        "id": "directory_secret_wins_over_the_login_secret",
        "env": {
            "DFE_TEST_ENTRA_LOGIN_SECRET": "login-secret",
            "DFE_TEST_ENTRA_SECRET": "group-secret",
        },
        "expected_credentials": make_graph_credentials(
            client_id="cid", client_secret="group-secret", tenant_id="tid"
        ),
        "provider": make_oidc_provider(
            client_id="cid",
            client_secret_env="DFE_TEST_ENTRA_LOGIN_SECRET",
            groups={
                "client_secret_env": "DFE_TEST_ENTRA_SECRET",
                "mode": "api",
                "tenant_id": "tid",
            },
            issuer=ENTRA_ISSUER,
            type="entra_id",
        ),
    },
    {
        "id": "falls_back_to_the_login_secret",
        "env": {"DFE_TEST_ENTRA_LOGIN_SECRET": "login-secret"},
        "expected_credentials": make_graph_credentials(
            client_id="cid", client_secret="login-secret", tenant_id="tid"
        ),
        "provider": make_oidc_provider(
            client_id="cid",
            client_secret_env="DFE_TEST_ENTRA_LOGIN_SECRET",
            groups={"mode": "api", "tenant_id": "tid"},
            issuer=ENTRA_ISSUER,
            type="entra_id",
        ),
    },
    {
        "id": "nothing_configured",
        "env": {},
        "expected_credentials": make_graph_credentials(
            client_id="", client_secret="", tenant_id=""
        ),
        "provider": make_oidc_provider(
            groups={"mode": "api"}, issuer=ENTRA_ISSUER, type="entra_id"
        ),
    },
]
