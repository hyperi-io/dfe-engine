#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/entra_adapter_cases.py
#  Purpose:      Case tables for the Entra ID adapter's Graph credential resolution and group listing
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Case tables for the Entra ID adapter's Graph credential resolution and group listing."""

import json
from typing import TypedDict

from dfe_engine.auth.oidc.adapters.entra import GraphCredentials
from dfe_engine.auth.oidc.models import GroupInfo, OIDCProvider
from tests.unit.test_auth.factories import (
    make_directory_reply,
    make_graph_credentials,
    make_group_info,
    make_oidc_provider,
)
from tests.unit.test_auth.test_oidc.local_directory import DirectoryReply

ENTRA_ISSUER = "https://login.microsoftonline.com/tid/v2.0"
FIRST_PAGE = "/v1.0/groups?$top=2"
SECOND_PAGE = "/v1.0/groups?$top=2&$skiptoken=p2"

_ONE = {"description": "First", "displayName": "One", "id": "g-one", "mail": "one@example.com"}
_TWO = {"description": None, "displayName": "Two", "id": "g-two", "mail": None}
_PAGE_ONE_OF_TWO = make_directory_reply(
    body=json.dumps({"@odata.nextLink": "{base}" + SECOND_PAGE, "value": [_ONE]})
)


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


class ConnectionCheckCase(TypedDict):
    id: str
    replies: dict[str, DirectoryReply]
    expected_result: tuple[bool, str]


class ListAllGroupsCase(TypedDict):
    id: str
    replies: dict[str, DirectoryReply]
    expected_groups: list[GroupInfo]


class ListAllGroupsMissingCredentialCase(TypedDict):
    id: str
    provider: OIDCProvider
    message: str


class ListAllGroupsRaisesCase(TypedDict):
    id: str
    replies: dict[str, DirectoryReply]
    message: str


CONNECTION_CHECK_CASES: list[ConnectionCheckCase] = [
    {
        "id": "a_group_list",
        "replies": {
            FIRST_PAGE: make_directory_reply(body=json.dumps({"value": [{"id": "g-one"}]}))
        },
        "expected_result": (True, "Graph API connection successful -- 1 group(s) returned"),
    },
    {
        "id": "body_not_a_list",
        "replies": {FIRST_PAGE: make_directory_reply(body=json.dumps({"value": "g-one"}))},
        "expected_result": (
            False,
            "Graph API connection failed: GET '/v1.0/groups' returned a body that is not a list "
            "of groups",
        ),
    },
    {
        "id": "forbidden",
        "replies": {
            FIRST_PAGE: make_directory_reply(
                body=json.dumps(
                    {
                        "error": {
                            "code": "Authorization_RequestDenied",
                            "message": "Insufficient privileges to complete the operation.",
                        }
                    }
                ),
                status=403,
            )
        },
        "expected_result": (
            False,
            "Graph API connection failed: GET '/v1.0/groups' returned HTTP 403: "
            "'Insufficient privileges to complete the operation.'",
        ),
    },
]

LIST_ALL_GROUPS_CASES: list[ListAllGroupsCase] = [
    {
        "id": "empty_directory",
        "replies": {FIRST_PAGE: make_directory_reply(body=json.dumps({"value": []}))},
        "expected_groups": [],
    },
    {
        "id": "one_page",
        "replies": {FIRST_PAGE: make_directory_reply(body=json.dumps({"value": [_ONE, _TWO]}))},
        "expected_groups": [
            make_group_info(description="First", email="one@example.com", id="g-one", name="One"),
            make_group_info(id="g-two", name="Two"),
        ],
    },
    {
        "id": "two_pages",
        "replies": {
            FIRST_PAGE: _PAGE_ONE_OF_TWO,
            SECOND_PAGE: make_directory_reply(body=json.dumps({"value": [_TWO]})),
        },
        "expected_groups": [
            make_group_info(description="First", email="one@example.com", id="g-one", name="One"),
            make_group_info(id="g-two", name="Two"),
        ],
    },
]

LIST_ALL_GROUPS_MISSING_CREDENTIAL_CASES: list[ListAllGroupsMissingCredentialCase] = [
    {
        "id": "nothing_configured",
        "provider": make_oidc_provider(
            groups={"mode": "api"}, issuer=ENTRA_ISSUER, type="entra_id"
        ),
        "message": "entra_id directory: credentials not configured, missing 'groups.tenant_id', "
        "'client_id', 'groups.client_secret'",
    },
    {
        "id": "no_client_secret",
        "provider": make_oidc_provider(
            client_id="dfe-sync-app",
            groups={"mode": "api", "tenant_id": "00000000-0000-0000-0000-000000000001"},
            issuer=ENTRA_ISSUER,
            type="entra_id",
        ),
        "message": "entra_id directory: credentials not configured, missing 'groups.client_secret'",
    },
]

LIST_ALL_GROUPS_RAISES_CASES: list[ListAllGroupsRaisesCase] = [
    {
        "id": "unauthorized",
        "replies": {
            FIRST_PAGE: make_directory_reply(
                body=json.dumps(
                    {
                        "error": {
                            "code": "InvalidAuthenticationToken",
                            "message": "Access token validation failure. Invalid audience.",
                        }
                    }
                ),
                status=401,
            )
        },
        "message": "entra_id directory: GET '/v1.0/groups' returned HTTP 401: "
        "'Access token validation failure. Invalid audience.'",
    },
    {
        "id": "forbidden",
        "replies": {
            FIRST_PAGE: make_directory_reply(
                body=json.dumps(
                    {
                        "error": {
                            "code": "Authorization_RequestDenied",
                            "message": "Insufficient privileges to complete the operation.",
                        }
                    }
                ),
                status=403,
            )
        },
        "message": "entra_id directory: GET '/v1.0/groups' returned HTTP 403: "
        "'Insufficient privileges to complete the operation.'",
    },
    {
        "id": "server_error",
        "replies": {FIRST_PAGE: make_directory_reply(body="upstream failed", status=500)},
        "message": "entra_id directory: GET '/v1.0/groups' returned HTTP 500",
    },
    {
        "id": "body_not_json",
        "replies": {FIRST_PAGE: make_directory_reply(body="<html>maintenance</html>")},
        "message": "entra_id directory: GET '/v1.0/groups' returned a body that is not JSON",
    },
    {
        "id": "body_not_an_object",
        "replies": {FIRST_PAGE: make_directory_reply(body=json.dumps([_ONE]))},
        "message": "entra_id directory: GET '/v1.0/groups' returned a body that is not a list "
        "of groups",
    },
    {
        "id": "group_not_an_object",
        "replies": {FIRST_PAGE: make_directory_reply(body=json.dumps({"value": ["g-one"]}))},
        "message": "entra_id directory: GET '/v1.0/groups' returned a body that is not a list "
        "of groups",
    },
    {
        "id": "second_page_refused",
        "replies": {
            FIRST_PAGE: _PAGE_ONE_OF_TWO,
            SECOND_PAGE: make_directory_reply(
                body=json.dumps(
                    {"error": {"code": "TooManyRequests", "message": "Too many requests."}}
                ),
                status=429,
            ),
        },
        "message": "entra_id directory: GET '/v1.0/groups' returned HTTP 429: 'Too many requests.'",
    },
]
