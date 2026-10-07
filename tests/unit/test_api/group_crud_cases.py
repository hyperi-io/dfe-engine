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
        "current": {"source_id": "dfe-admins", "source_provider": ""},
        "change": {"source_id": "mallory-group"},
    },
    {
        "id": "source_id_cleared",
        "current": {"source_id": "dfe-admins", "source_provider": ""},
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


UNLOADABLE_PROVIDER_FILE_CASES: list[UnloadableProviderFileCase] = [
    {"id": "malformed_yaml", "content": b"type: [generic\n"},
    {"id": "not_a_provider", "content": b"type: not-a-provider-type\n"},
    {"id": "a_list", "content": b"- generic\n- okta\n"},
    {"id": "not_utf_8", "content": b"\xff\xfe\xfa"},
    {"id": "invalid_timestamp", "content": b"last_sync_at: 2026-13-45\n"},
    {"id": "int_past_the_digit_limit", "content": b"sync_interval: " + b"9" * 5000 + b"\n"},
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
    {
        "id": "legacy_link_moved_without_a_provider",
        "current": {"source_id": "dfe-viewers", "source_provider": ""},
        "change": {"source_id": "okta-viewers"},
    },
]
