#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/google_adapter_cases.py
#  Purpose:      Case tables for the Google Workspace adapter's group listing
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Case tables for the Google Workspace adapter's group listing."""

import json
from typing import TypedDict

from dfe_engine.auth.oidc.models import GroupInfo, OIDCProvider
from tests.unit.test_auth.factories import (
    make_directory_reply,
    make_group_info,
    make_oidc_provider,
)
from tests.unit.test_auth.test_oidc.local_directory import DirectoryReply

FIRST_PAGE = "/admin/directory/v1/groups?maxResults=200&domain=example.com&alt=json"
GOOGLE_ISSUER = "https://accounts.google.com"
SECOND_PAGE = "/admin/directory/v1/groups?maxResults=200&domain=example.com&pageToken=p2&alt=json"
TOKEN_PATH = "/token"
TOKEN_REPLY = make_directory_reply(
    body=json.dumps(
        {"access_token": "test-google-token", "expires_in": 3600, "token_type": "Bearer"}
    )
)

_ONE = {"email": "one@example.com", "id": "g-one", "name": "One"}
_TWO = {"id": "g-two", "name": "Two"}
_PAGE_ONE_OF_TWO = make_directory_reply(body=json.dumps({"groups": [_ONE], "nextPageToken": "p2"}))


class ListAllGroupsCase(TypedDict):
    id: str
    replies: dict[str, DirectoryReply]
    expected_groups: list[GroupInfo]


class ListAllGroupsMissingServiceAccountCase(TypedDict):
    id: str
    env: dict[str, str]
    provider: OIDCProvider
    message: str


class ListAllGroupsRaisesCase(TypedDict):
    id: str
    replies: dict[str, DirectoryReply]
    message: str


LIST_ALL_GROUPS_CASES: list[ListAllGroupsCase] = [
    {
        # The Admin SDK leaves the groups key out of a domain with no groups.
        "id": "empty_directory",
        "replies": {
            FIRST_PAGE: make_directory_reply(body=json.dumps({"kind": "admin#directory#groups"}))
        },
        "expected_groups": [],
    },
    {
        "id": "one_page",
        "replies": {FIRST_PAGE: make_directory_reply(body=json.dumps({"groups": [_ONE, _TWO]}))},
        "expected_groups": [
            make_group_info(email="one@example.com", id="g-one", name="One"),
            make_group_info(id="g-two", name="Two"),
        ],
    },
    {
        "id": "two_pages",
        "replies": {
            FIRST_PAGE: _PAGE_ONE_OF_TWO,
            SECOND_PAGE: make_directory_reply(body=json.dumps({"groups": [_TWO]})),
        },
        "expected_groups": [
            make_group_info(email="one@example.com", id="g-one", name="One"),
            make_group_info(id="g-two", name="Two"),
        ],
    },
]

LIST_ALL_GROUPS_MISSING_SERVICE_ACCOUNT_CASES: list[ListAllGroupsMissingServiceAccountCase] = [
    {
        "id": "no_env_named",
        "env": {},
        "provider": make_oidc_provider(groups={"mode": "api"}, issuer=GOOGLE_ISSUER, type="google"),
        "message": "google directory: no service account; send 'groups.service_account_json' to "
        "the provider API or set an env var named in 'groups.service_account_json_env'",
    },
    {
        "id": "env_unset",
        "env": {},
        "provider": make_oidc_provider(
            groups={"mode": "api", "service_account_json_env": "DFE_TEST_GOOGLE_SA_NEVER_SET"},
            issuer=GOOGLE_ISSUER,
            type="google",
        ),
        "message": "google directory: no service account; send 'groups.service_account_json' to "
        "the provider API or set the env var 'DFE_TEST_GOOGLE_SA_NEVER_SET'",
    },
    {
        "id": "not_json",
        "env": {"DFE_TEST_GOOGLE_SA_JSON": "not-json"},
        "provider": make_oidc_provider(
            groups={"mode": "api", "service_account_json_env": "DFE_TEST_GOOGLE_SA_JSON"},
            issuer=GOOGLE_ISSUER,
            type="google",
        ),
        "message": "google directory: the service account JSON does not load; the reason is in "
        "the engine log",
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
                            "code": 401,
                            "message": "Request had invalid authentication credentials.",
                        }
                    }
                ),
                status=401,
            )
        },
        "message": "google directory: GET '/admin/directory/v1/groups' returned HTTP 401: "
        "'Request had invalid authentication credentials.'",
    },
    {
        "id": "forbidden",
        "replies": {
            FIRST_PAGE: make_directory_reply(
                body=json.dumps(
                    {
                        "error": {
                            "code": 403,
                            "message": "Not Authorized to access this resource/api",
                        }
                    }
                ),
                status=403,
            )
        },
        "message": "google directory: GET '/admin/directory/v1/groups' returned HTTP 403: "
        "'Not Authorized to access this resource/api'",
    },
    {
        "id": "server_error",
        "replies": {FIRST_PAGE: make_directory_reply(body="upstream failed", status=500)},
        "message": "google directory: GET '/admin/directory/v1/groups' returned HTTP 500: "
        "'Internal Server Error'",
    },
    {
        "id": "body_not_json",
        "replies": {FIRST_PAGE: make_directory_reply(body="<html>maintenance</html>")},
        "message": "google directory: GET '/admin/directory/v1/groups' returned a body that is "
        "not a list of groups",
    },
    {
        "id": "groups_not_a_list",
        "replies": {FIRST_PAGE: make_directory_reply(body=json.dumps({"groups": _ONE}))},
        "message": "google directory: GET '/admin/directory/v1/groups' returned a body that is "
        "not a list of groups",
    },
    {
        "id": "group_without_a_name",
        "replies": {FIRST_PAGE: make_directory_reply(body=json.dumps({"groups": [{"id": "g-x"}]}))},
        "message": "google directory: the groups listing returned a group without a string id "
        "and name",
    },
    {
        "id": "second_page_refused",
        "replies": {
            FIRST_PAGE: _PAGE_ONE_OF_TWO,
            SECOND_PAGE: make_directory_reply(
                body=json.dumps({"error": {"code": 429, "message": "Quota exceeded."}}),
                status=429,
            ),
        },
        "message": "google directory: GET '/admin/directory/v1/groups' returned HTTP 429: "
        "'Quota exceeded.'",
    },
]
