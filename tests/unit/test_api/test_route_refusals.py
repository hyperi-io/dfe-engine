#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_route_refusals.py
#  Purpose:      One table of the routes a viewer is refused and the ids that answer 404
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A viewer is refused with 403 and an id nothing holds answers 404, one table row each.

A row is ``(method, path, json body)``. The no-token 401 is in ``test_openapi_auth_errors.py``, which reads the routes from the app.
"""

import pytest

# Each route needs a grant the viewer account does not hold.
VIEWER_REFUSED = [
    ("post", "/api/v1/auth/accounts", {"username": "blocked", "password": "pw"}),
    ("get", "/api/v1/auth/accounts", None),
    ("get", "/api/v1/auth/accounts/admin", None),
    ("put", "/api/v1/auth/accounts/admin", {"enabled": False}),
    ("post", "/api/v1/auth/accounts/admin/reset-password", {"new_password": "pw"}),
    (
        "post",
        "/api/v1/auth/accounts/admin/rotate-password",
        {"new_password": "a-newly-minted-password"},
    ),
    ("delete", "/api/v1/auth/accounts/admin", None),
    ("get", "/api/v1/auth/accounts/admin/attributes", None),
    ("put", "/api/v1/auth/accounts/admin/attributes", {"attributes": {"x": 1}}),
    ("get", "/api/v1/auth/accounts/admin/sensitive-attributes", None),
    ("post", "/api/v1/auth/api-keys", {"name": "blocked-key"}),
    ("get", "/api/v1/auth/api-keys", None),
    ("delete", "/api/v1/auth/api-keys/deadbeef", None),
    ("post", "/api/v1/auth/groups", {"name": "blocked", "roles": ["admin"]}),
    ("put", "/api/v1/auth/groups/dfe-viewers", {"roles": ["admin"]}),
    ("post", "/api/v1/auth/groups/dfe-viewers/members", {"username": "admin"}),
    ("delete", "/api/v1/auth/groups/dfe-viewers/members/viewer", None),
    ("delete", "/api/v1/auth/groups/dfe-viewers", None),
    ("post", "/api/v1/auth/oidc-providers", {"name": "blocked", "type": "generic"}),
    ("get", "/api/v1/auth/oidc-providers", None),
    ("get", "/api/v1/auth/oidc-providers/anything", None),
    ("put", "/api/v1/auth/oidc-providers/anything", {"enabled": False}),
    ("delete", "/api/v1/auth/oidc-providers/anything", None),
    ("post", "/api/v1/auth/oidc-providers/anything/sync", None),
    ("get", "/api/v1/auth/oidc-providers/anything/test", None),
    ("get", "/api/v1/auth/oidc-providers/anything/verify-login", None),
    ("get", "/api/v1/auth/roles/scopes", None),
    ("post", "/api/v1/governance/ch-rbac/reconcile", None),
    ("post", "/api/v1/orgs", {"name": "acme"}),
    ("post", "/api/v1/orgs", {"name": "viewer-attempt", "org_ids": ["va"]}),
    (
        "post",
        "/api/v1/rules",
        {"name": "viewer_rule", "severity": "low", "user_sql": "event_type = 'login'"},
    ),
    ("post", "/api/v1/rules/validate", {"sql": "event_type = 'login'"}),
    ("post", "/api/v1/services/seed", None),
    ("post", "/api/v1/synthetic-data/lookalike", {"rows": [{"a": 1}], "count": 1}),
    ("get", "/api/v1/system/defaults/drift", None),
    ("post", "/api/v1/system/defaults/apply", {"sources": ["alpha"]}),
]

# A path or id nothing serves or holds, asked by an admin.
ADMIN_NOT_FOUND = [
    ("put", "/api/v1/auth/accounts/ghost", {"enabled": False}),
    ("delete", "/api/v1/auth/accounts/ghost", None),
    ("put", "/api/v1/auth/accounts/ghost/attributes", {"attributes": {"x": 1}}),
    ("get", "/api/v1/auth/accounts/ghost/sensitive-attributes", None),
    ("put", "/api/v1/auth/groups/ghost", {"roles": ["admin"]}),
    ("post", "/api/v1/auth/groups/ghost/members", {"username": "admin"}),
    ("delete", "/api/v1/auth/groups/ghost/members/admin", None),
    ("delete", "/api/v1/auth/groups/ghost", None),
    ("put", "/api/v1/auth/groups/ghostgroup/attributes", {"attributes": {"x": 1}}),
    ("get", "/api/v1/auth/groups/ghostgroup/sensitive-attributes", None),
    ("put", "/api/v1/auth/oidc-providers/ghost", {"enabled": False}),
    ("delete", "/api/v1/auth/oidc-providers/nonexistent", None),
    ("post", "/api/v1/auth/oidc-providers/ghost/sync", None),
    ("get", "/api/v1/auth/oidc-providers/ghost/test", None),
    ("get", "/api/v1/auth/oidc-providers/ghost/verify-login", None),
    ("put", "/api/v1/orgs/nobody", {"display_name": "?"}),
    ("put", "/api/v1/orgs/no-such-org", {"display_name": "X"}),
    ("delete", "/api/v1/orgs/nobody", None),
    ("delete", "/api/v1/orgs/ghost-xyz", None),
    ("get", "/api/v1/discovery/databases", None),
    ("get", "/api/v1/discovery/tables", None),
    ("get", "/api/v1/discovery/tables/my_table/columns", None),
    ("get", "/api/v1/operations/nonexistent-id/stream", None),
    ("post", "/api/v1/operations/nonexistent-id/cancel", None),
    ("get", "/api/v1/samples/does-not-exist", None),
    ("delete", "/api/v1/services/receiver/production", None),
    ("get", "/api/v1/sources/nonexistent", None),
    (
        "put",
        "/api/v1/sources/nonexistent",
        {
            "source": "nonexistent",
            "match": {"field": "tags.collector.type", "value": "nonexistent"},
        },
    ),
    ("delete", "/api/v1/sources/nonexistent", None),
    ("get", "/api/v1/sources/nonexistent/columns", None),
    ("post", "/api/v1/sources/nonexistent/build", None),
    ("post", "/api/v1/sources/nonexistent/deploy", None),
    ("get", "/api/v1/sources/nonexistent/flow", None),
    ("get", "/api/v1/sources/missing/versions/1.0.0", None),
]


def _id(row: tuple[str, str, object]) -> str:
    return f"{row[0].upper()} {row[1]}"


@pytest.mark.parametrize("row", VIEWER_REFUSED, ids=_id)
def test_a_viewer_is_refused(client, viewer_headers, row):
    method, path, body = row

    assert client.request(method, path, json=body, headers=viewer_headers).status_code == 403


@pytest.mark.parametrize("row", ADMIN_NOT_FOUND, ids=_id)
def test_an_id_nothing_holds_is_not_found(client, admin_headers, row):
    method, path, body = row

    assert client.request(method, path, json=body, headers=admin_headers).status_code == 404
