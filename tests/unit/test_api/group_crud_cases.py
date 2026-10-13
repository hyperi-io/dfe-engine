#  Project:      dfe-engine
#  File:         tests/unit/test_api/group_crud_cases.py
#  Purpose:      Case tables for the group CRUD REST endpoint tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Case tables for the group CRUD REST endpoint tests."""

from typing import TypedDict


class KnownSourceProviderCase(TypedDict):
    id: str
    source_provider: str


KNOWN_SOURCE_PROVIDER_CASES: list[KnownSourceProviderCase] = [
    {"id": "the_proxy_provider", "source_provider": "oidc"},
    {"id": "scim", "source_provider": "scim"},
    {"id": "a_registered_oidc_provider", "source_provider": "okta"},
    {"id": "a_bound_stamp", "source_provider": "okta-scim"},
    {"id": "a_provider_a_stamp_is_bound_to", "source_provider": "entra"},
]


class LinkChangeCase(TypedDict):
    id: str
    current: dict[str, str]
    change: dict[str, str]


LINK_CHANGE_CASES: list[LinkChangeCase] = [
    {
        "id": "source_id_newly_set",
        "current": {"source_id": "", "source_provider": ""},
        "change": {"source_id": "mallory-group"},
    },
    {
        "id": "source_id_changed",
        "current": {"source_id": "dfe-admins", "source_provider": "oidc"},
        "change": {"source_id": "mallory-group"},
    },
    {
        "id": "source_id_cleared",
        "current": {"source_id": "dfe-admins", "source_provider": "oidc"},
        "change": {"source_id": ""},
    },
    {
        "id": "source_provider_changed",
        "current": {"source_id": "dfe-admins", "source_provider": "scim"},
        "change": {"source_provider": "oidc"},
    },
    {
        "id": "source_provider_widened_to_any",
        "current": {"source_id": "dfe-admins", "source_provider": "scim"},
        "change": {"source_provider": ""},
    },
]


class MalformedSourceIdCase(TypedDict):
    id: str
    source_id: str


MALFORMED_SOURCE_ID_CASES: list[MalformedSourceIdCase] = [
    {"id": "leading_space", "source_id": " dfe-viewers"},
    {"id": "trailing_space", "source_id": "dfe-viewers "},
    {"id": "trailing_newline", "source_id": "dfe-viewers\n"},
    {"id": "leading_tab", "source_id": "\tdfe-viewers"},
    {"id": "past_512_characters", "source_id": "g" * 513},
    {"id": "nul_inside", "source_id": "dfe\x00viewers"},
    {"id": "escape_inside", "source_id": "dfe\x1bviewers"},
]


class UnloadableProviderFileCase(TypedDict):
    id: str
    content: bytes
    expected_error_type: str


UNLOADABLE_PROVIDER_FILE_CASES: list[UnloadableProviderFileCase] = [
    {"id": "malformed_yaml", "content": b"type: [generic\n", "expected_error_type": "ParserError"},
    {
        "id": "not_a_provider",
        "content": b"type: not-a-provider-type\n",
        "expected_error_type": "ValidationError",
    },
    {"id": "a_list", "content": b"- generic\n- okta\n", "expected_error_type": "ValidationError"},
    {"id": "not_utf_8", "content": b"\xff\xfe\xfa", "expected_error_type": "UnicodeDecodeError"},
    {
        "id": "invalid_timestamp",
        "content": b"last_sync_at: 2026-13-45\n",
        "expected_error_type": "ValueError",
    },
    {
        "id": "int_past_the_digit_limit",
        "content": b"sync_interval: " + b"9" * 5000 + b"\n",
        "expected_error_type": "ValueError",
    },
]


class GroupDeleteRouteCase(TypedDict):
    id: str
    route: str


GROUP_DELETE_ROUTE_CASES: list[GroupDeleteRouteCase] = [
    {"id": "groups_api", "route": "/api/v1/auth/groups"},
    {"id": "scim", "route": "/api/v1/scim/v2/Groups"},
]


class MissingProviderCase(TypedDict):
    id: str
    current: dict[str, str]
    change: dict[str, str]


MISSING_PROVIDER_CASES: list[MissingProviderCase] = [
    {
        "id": "source_id_set_without_a_provider",
        "current": {"source_id": "", "source_provider": ""},
        "change": {"source_id": "dfe-viewers"},
    },
    {
        "id": "provider_cleared_from_a_link",
        "current": {"source_id": "dfe-viewers", "source_provider": "oidc"},
        "change": {"source_provider": ""},
    },
]


# The IdP-owned account IDP_MEMBER_WRITE_CASES write into groups; ADMINS_SOURCE_ID is the IdP group id dfe-admins is linked to.
IDP_ACCOUNT = "jane-corp-com"
ADMINS_SOURCE_ID = "DFE-Admins"

_PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
_SCIM_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"


class IdpMemberWriteCase(TypedDict):
    id: str
    caller: str
    stored_groups: list[str]
    method: str
    route: str
    body: dict[str, object]
    expected_status: int


IDP_MEMBER_WRITE_CASES: list[IdpMemberWriteCase] = [
    {
        "id": "groups_api_create_named_like_a_source_id",
        "caller": "operator",
        "stored_groups": [],
        "method": "POST",
        "route": "/api/v1/auth/groups",
        "body": {"members": [IDP_ACCOUNT], "name": ADMINS_SOURCE_ID, "roles": []},
        "expected_status": 409,
    },
    {
        "id": "groups_api_create_with_an_idp_member",
        "caller": "operator",
        "stored_groups": [],
        "method": "POST",
        "route": "/api/v1/auth/groups",
        "body": {"members": [IDP_ACCOUNT], "name": "audit-team", "roles": []},
        "expected_status": 201,
    },
    {
        "id": "groups_api_add_member",
        "caller": "operator",
        "stored_groups": [ADMINS_SOURCE_ID],
        "method": "POST",
        "route": f"/api/v1/auth/groups/{ADMINS_SOURCE_ID}/members",
        "body": {"username": IDP_ACCOUNT},
        "expected_status": 200,
    },
    {
        "id": "groups_api_put_members",
        "caller": "operator",
        "stored_groups": [ADMINS_SOURCE_ID],
        "method": "PUT",
        "route": f"/api/v1/auth/groups/{ADMINS_SOURCE_ID}",
        "body": {"members": [IDP_ACCOUNT]},
        "expected_status": 200,
    },
    {
        "id": "org_scoped_create_named_like_a_source_id",
        "caller": "orgop",
        "stored_groups": [],
        "method": "POST",
        "route": "/api/v1/auth/groups",
        "body": {
            "members": [IDP_ACCOUNT],
            "name": ADMINS_SOURCE_ID,
            "roles": [],
            "scope": "org:acme",
        },
        "expected_status": 409,
    },
    {
        "id": "org_scoped_create_with_an_idp_member",
        "caller": "orgop",
        "stored_groups": [],
        "method": "POST",
        "route": "/api/v1/auth/groups",
        "body": {"members": [IDP_ACCOUNT], "name": "acme-audit", "roles": [], "scope": "org:acme"},
        "expected_status": 201,
    },
    {
        "id": "self_add_on_a_live_session",
        "caller": "jane",
        "stored_groups": [ADMINS_SOURCE_ID],
        "method": "POST",
        "route": f"/api/v1/auth/groups/{ADMINS_SOURCE_ID}/members",
        "body": {"username": IDP_ACCOUNT},
        "expected_status": 200,
    },
    {
        "id": "scim_post_named_like_a_source_id",
        "caller": "operator",
        "stored_groups": [],
        "method": "POST",
        "route": "/api/v1/scim/v2/Groups",
        "body": {
            "displayName": ADMINS_SOURCE_ID,
            "members": [{"value": IDP_ACCOUNT}],
            "schemas": [_SCIM_GROUP_SCHEMA],
        },
        "expected_status": 409,
    },
    {
        "id": "scim_post_with_an_idp_member",
        "caller": "operator",
        "stored_groups": [],
        "method": "POST",
        "route": "/api/v1/scim/v2/Groups",
        "body": {
            "displayName": "audit-team",
            "members": [{"value": IDP_ACCOUNT}],
            "schemas": [_SCIM_GROUP_SCHEMA],
        },
        "expected_status": 201,
    },
    {
        "id": "scim_patch_add_member",
        "caller": "operator",
        "stored_groups": [ADMINS_SOURCE_ID],
        "method": "PATCH",
        "route": f"/api/v1/scim/v2/Groups/{ADMINS_SOURCE_ID}",
        "body": {
            "Operations": [{"op": "add", "path": "members", "value": [{"value": IDP_ACCOUNT}]}],
            "schemas": [_PATCH_SCHEMA],
        },
        "expected_status": 200,
    },
    {
        "id": "scim_put_members",
        "caller": "operator",
        "stored_groups": [ADMINS_SOURCE_ID],
        "method": "PUT",
        "route": f"/api/v1/scim/v2/Groups/{ADMINS_SOURCE_ID}",
        "body": {
            "displayName": ADMINS_SOURCE_ID,
            "members": [{"value": IDP_ACCOUNT}],
            "schemas": [_SCIM_GROUP_SCHEMA],
        },
        "expected_status": 200,
    },
]


class IdentifierCollisionCase(TypedDict):
    id: str
    method: str
    route: str
    body: dict[str, object]
    expected_status: int
    expected_error: dict[str, str]


# dfe-admins carries ADMINS_SOURCE_ID; sec-team is an unlinked group with no members.
IDENTIFIER_COLLISION_CASES: list[IdentifierCollisionCase] = [
    {
        "id": "groups_api_create_named_like_another_groups_source_id",
        "method": "POST",
        "route": "/api/v1/auth/groups",
        "body": {"name": ADMINS_SOURCE_ID, "roles": []},
        "expected_status": 409,
        "expected_error": {"code": "identifier_taken"},
    },
    {
        "id": "groups_api_create_with_another_groups_name_as_source_id",
        "method": "POST",
        "route": "/api/v1/auth/groups",
        "body": {
            "name": "shadow",
            "roles": [],
            "source_id": "dfe-viewers",
            "source_provider": "oidc",
        },
        "expected_status": 409,
        "expected_error": {"code": "identifier_taken"},
    },
    {
        "id": "groups_api_update_to_another_groups_name_as_source_id",
        "method": "PUT",
        "route": "/api/v1/auth/groups/sec-team",
        "body": {"source_id": "dfe-viewers", "source_provider": "oidc"},
        "expected_status": 409,
        "expected_error": {"code": "identifier_taken"},
    },
    {
        "id": "groups_api_create_named_like_its_own_source_id",
        "method": "POST",
        "route": "/api/v1/auth/groups",
        "body": {
            "name": "okta-soc",
            "roles": [],
            "source_id": "okta-soc",
            "source_provider": "oidc",
        },
        "expected_status": 201,
        "expected_error": {},
    },
    {
        "id": "scim_post_named_like_another_groups_source_id",
        "method": "POST",
        "route": "/api/v1/scim/v2/Groups",
        "body": {"displayName": ADMINS_SOURCE_ID, "members": [], "schemas": [_SCIM_GROUP_SCHEMA]},
        "expected_status": 409,
        "expected_error": {"scimType": "uniqueness"},
    },
    {
        "id": "scim_post_with_another_groups_name_as_external_id",
        "method": "POST",
        "route": "/api/v1/scim/v2/Groups",
        "body": {
            "displayName": "shadow",
            "externalId": "dfe-viewers",
            "members": [],
            "schemas": [_SCIM_GROUP_SCHEMA],
        },
        "expected_status": 409,
        "expected_error": {"scimType": "uniqueness"},
    },
    {
        "id": "scim_put_to_another_groups_name_as_external_id",
        "method": "PUT",
        "route": "/api/v1/scim/v2/Groups/sec-team",
        "body": {
            "displayName": "sec-team",
            "externalId": "dfe-viewers",
            "members": [],
            "schemas": [_SCIM_GROUP_SCHEMA],
        },
        "expected_status": 409,
        "expected_error": {"scimType": "uniqueness"},
    },
]
