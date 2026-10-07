#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_oidc/okta_adapter_cases.py
#  Purpose:      Case tables for the Okta adapter tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Case tables for the Okta adapter tests."""

import json
from collections.abc import Awaitable, Callable
from operator import methodcaller
from typing import Any, TypedDict

from dfe_engine.auth.oidc.adapters.okta import OktaAdapter
from dfe_engine.auth.oidc.models import GroupInfo, OIDCProvider
from tests.unit.test_auth.factories import (
    make_directory_reply,
    make_group_info,
    make_oidc_provider,
)
from tests.unit.test_auth.test_oidc.local_directory import DirectoryReply

FIRST_PAGE = "/api/v1/groups?limit=200"
SECOND_PAGE = "/api/v1/groups?after=00g-one&limit=200"

_ONE = {"id": "00g-one", "profile": {"description": "First", "name": "One"}}
_TWO = {"id": "00g-two", "profile": {"name": "Two"}}
_PAGE_ONE_OF_TWO = make_directory_reply(
    body=json.dumps([_ONE]),
    headers=[("Link", '<{base}/api/v1/groups?after=00g-one&limit=200>; rel="next"')],
)
_SUMMARY_200 = "Invalid token provided " + "x" * 177
_TOKEN_REFUSED = (
    "Okta API token holds a character an HTTP header cannot carry: a space, a control "
    "character or a non-ASCII one; re-send 'groups.api_token' to the provider API or fix "
    "the env var 'DFE_TEST_OKTA_API_TOKEN'"
)
# The fragment of each unsendable token a log record or an answer must never carry.
TOKEN_FRAGMENT = "value-7f3a"


class ConnectionCheckCase(TypedDict):
    id: str
    replies: dict[str, DirectoryReply]
    expected_result: tuple[bool, str]


class UnsendableTokenCase(TypedDict):
    id: str
    token: str
    call: Callable[[OktaAdapter], Awaitable[Any]]
    expected_result: Any


class ListAllGroupsCase(TypedDict):
    id: str
    replies: dict[str, DirectoryReply]
    expected_groups: list[GroupInfo]


class ListAllGroupsMissingSettingCase(TypedDict):
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
        "replies": {"/api/v1/groups?limit=1": make_directory_reply(body=json.dumps([_ONE]))},
        "expected_result": (True, "Okta Groups API connection successful -- 1 group(s) returned"),
    },
    {
        "id": "body_not_a_list",
        "replies": {
            "/api/v1/groups?limit=1": make_directory_reply(
                body=json.dumps({"errorSummary": "Not a list"})
            )
        },
        "expected_result": (
            False,
            "Okta Groups API connection failed: GET '/api/v1/groups' returned a body that is not "
            "a list of groups",
        ),
    },
    {
        "id": "unauthorized",
        "replies": {
            "/api/v1/groups?limit=1": make_directory_reply(
                body=json.dumps({"errorSummary": "Invalid token provided"}), status=401
            )
        },
        "expected_result": (
            False,
            "Okta Groups API connection failed: GET '/api/v1/groups' returned HTTP 401: "
            "'Invalid token provided'",
        ),
    },
]

LIST_ALL_GROUPS_CASES: list[ListAllGroupsCase] = [
    {
        "id": "empty_directory",
        "replies": {FIRST_PAGE: make_directory_reply(body="[]")},
        "expected_groups": [],
    },
    {
        "id": "link_header_on_two_lines",
        "replies": {
            FIRST_PAGE: make_directory_reply(
                body=json.dumps([_ONE]),
                headers=[
                    ("Link", '<{base}/api/v1/groups?limit=200>; rel="self"'),
                    ("Link", '<{base}/api/v1/groups?after=00g-one&limit=200>; rel="next"'),
                ],
            ),
            SECOND_PAGE: make_directory_reply(body=json.dumps([_TWO])),
        },
        "expected_groups": [
            make_group_info(description="First", id="00g-one", name="One"),
            make_group_info(id="00g-two", name="Two"),
        ],
    },
    {
        "id": "one_page",
        "replies": {FIRST_PAGE: make_directory_reply(body=json.dumps([_ONE, _TWO]))},
        "expected_groups": [
            make_group_info(description="First", id="00g-one", name="One"),
            make_group_info(id="00g-two", name="Two"),
        ],
    },
    {
        "id": "two_pages",
        "replies": {
            FIRST_PAGE: _PAGE_ONE_OF_TWO,
            SECOND_PAGE: make_directory_reply(body=json.dumps([_TWO])),
        },
        "expected_groups": [
            make_group_info(description="First", id="00g-one", name="One"),
            make_group_info(id="00g-two", name="Two"),
        ],
    },
]

LIST_ALL_GROUPS_MISSING_SETTING_CASES: list[ListAllGroupsMissingSettingCase] = [
    {
        "id": "no_domain",
        "provider": make_oidc_provider(groups={"mode": "api"}, type="okta"),
        "message": "okta directory: 'groups.okta_domain' is not set",
    },
    {
        "id": "token_env_unset",
        "provider": make_oidc_provider(
            groups={
                "api_token_env": "DFE_TEST_OKTA_TOKEN_NEVER_SET",
                "mode": "api",
                "okta_domain": "example.okta.com",
            },
            type="okta",
        ),
        "message": "okta directory: no API token; send 'groups.api_token' to the provider API "
        "or set the env var 'DFE_TEST_OKTA_TOKEN_NEVER_SET'",
    },
    {
        "id": "no_token_env_named",
        "provider": make_oidc_provider(
            groups={"mode": "api", "okta_domain": "example.okta.com"}, type="okta"
        ),
        "message": "okta directory: no API token; send 'groups.api_token' to the provider API "
        "or set an env var named in 'groups.api_token_env'",
    },
]

LIST_ALL_GROUPS_RAISES_CASES: list[ListAllGroupsRaisesCase] = [
    {
        "id": "error_summary_cut_to_200_characters",
        "replies": {
            FIRST_PAGE: make_directory_reply(
                body=json.dumps({"errorSummary": _SUMMARY_200 + "y" * 50}), status=401
            )
        },
        "message": f"okta directory: GET '/api/v1/groups' returned HTTP 401: {_SUMMARY_200!r}",
    },
    {
        "id": "unauthorized",
        "replies": {
            FIRST_PAGE: make_directory_reply(
                body=json.dumps(
                    {"errorCode": "E0000011", "errorSummary": "Invalid token provided"}
                ),
                status=401,
            )
        },
        "message": "okta directory: GET '/api/v1/groups' returned HTTP 401: "
        "'Invalid token provided'",
    },
    {
        "id": "forbidden",
        "replies": {
            FIRST_PAGE: make_directory_reply(
                body=json.dumps(
                    {
                        "errorCode": "E0000006",
                        "errorSummary": "You do not have permission to perform the requested action",
                    }
                ),
                status=403,
            )
        },
        "message": "okta directory: GET '/api/v1/groups' returned HTTP 403: "
        "'You do not have permission to perform the requested action'",
    },
    {
        "id": "server_error",
        "replies": {FIRST_PAGE: make_directory_reply(body="upstream failed", status=500)},
        "message": "okta directory: GET '/api/v1/groups' returned HTTP 500",
    },
    {
        "id": "body_not_json",
        "replies": {FIRST_PAGE: make_directory_reply(body="<html>maintenance</html>")},
        "message": "okta directory: GET '/api/v1/groups' returned a body that is not JSON",
    },
    {
        "id": "body_not_a_list",
        "replies": {
            FIRST_PAGE: make_directory_reply(body=json.dumps({"errorSummary": "Not a list"}))
        },
        "message": "okta directory: GET '/api/v1/groups' returned a body that is not a list "
        "of groups",
    },
    {
        "id": "group_not_an_object",
        "replies": {FIRST_PAGE: make_directory_reply(body=json.dumps(["00g-one"]))},
        "message": "okta directory: GET '/api/v1/groups' returned a body that is not a list "
        "of groups",
    },
    {
        "id": "second_page_refused",
        "replies": {
            FIRST_PAGE: _PAGE_ONE_OF_TWO,
            SECOND_PAGE: make_directory_reply(
                body=json.dumps(
                    {
                        "errorCode": "E0000047",
                        "errorSummary": "API call exceeded rate limit due to too many requests.",
                    }
                ),
                status=429,
            ),
        },
        "message": "okta directory: GET '/api/v1/groups' returned HTTP 429: "
        "'API call exceeded rate limit due to too many requests.'",
    },
]

UNSENDABLE_TOKEN_CASES: list[UnsendableTokenCase] = [
    {
        "id": "line_break_test_connection",
        "token": "secret-okta\nvalue-7f3a",
        "call": methodcaller("test_connection"),
        "expected_result": (False, _TOKEN_REFUSED),
    },
    {
        "id": "line_break_resolve_groups",
        "token": "secret-okta\nvalue-7f3a",
        "call": methodcaller("resolve_groups", ["00g-one"]),
        "expected_result": {"00g-one": "00g-one"},
    },
    {
        "id": "line_break_resolve_user_groups",
        "token": "secret-okta\nvalue-7f3a",
        "call": methodcaller("resolve_user_groups", "00u-one"),
        "expected_result": [],
    },
    {
        "id": "byte_order_mark_test_connection",
        "token": "\ufeffsecret-okta-value-7f3a",
        "call": methodcaller("test_connection"),
        "expected_result": (False, _TOKEN_REFUSED),
    },
]
