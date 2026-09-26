#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_attributes_api.py
#  Purpose:      Tests for account/group attribute REST endpoints
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for the account/group non-sensitive and sensitive attribute endpoints.

The API test app runs the yaml backend, so sensitive attributes are served by
the yaml :class:`AttributeStore`. ``app.state.account_sensitive_attributes`` and
``app.state.group_sensitive_attributes`` are built by the real lifespan.
"""

from __future__ import annotations

import secrets

NESTED = {"team": "blue", "profile": {"tz": "Australia/Canberra", "tags": ["a", "b"]}}
SENSITIVE = {"otp_seed": "abc123", "recovery": {"codes": ["one", "two"]}}


class TestAccountAttributes:
    """/api/v1/auth/accounts/{username}/attributes"""

    def _make_account(self, client, admin_headers, username: str) -> None:
        client.post(
            "/api/v1/auth/accounts",
            json={
                "username": username,
                "password": secrets.token_urlsafe(16),
                "email": f"{username}@example.com",
            },
            headers=admin_headers,
        )

    def test_put_then_get_roundtrip(self, client, admin_headers):
        self._make_account(client, admin_headers, "attruser")
        put = client.put(
            "/api/v1/auth/accounts/attruser/attributes",
            json={"attributes": NESTED},
            headers=admin_headers,
        )
        assert put.status_code == 200
        assert put.json()["attributes"] == NESTED

        get = client.get(
            "/api/v1/auth/accounts/attruser/attributes",
            headers=admin_headers,
        )
        assert get.status_code == 200
        assert get.json()["attributes"] == NESTED

    def test_get_missing_account_returns_404(self, client, admin_headers):
        resp = client.get(
            "/api/v1/auth/accounts/ghost/attributes",
            headers=admin_headers,
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_put_missing_account_returns_404(self, client, admin_headers):
        resp = client.put(
            "/api/v1/auth/accounts/ghost/attributes",
            json={"attributes": {"x": 1}},
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_get_requires_scope(self, client, viewer_headers):
        resp = client.get(
            "/api/v1/auth/accounts/admin/attributes",
            headers=viewer_headers,
        )
        assert resp.status_code == 403

    def test_put_requires_scope(self, client, viewer_headers):
        resp = client.put(
            "/api/v1/auth/accounts/admin/attributes",
            json={"attributes": {"x": 1}},
            headers=viewer_headers,
        )
        assert resp.status_code == 403

    def test_sensitive_put_then_get_roundtrip(self, client, admin_headers):
        self._make_account(client, admin_headers, "sensuser")
        put = client.put(
            "/api/v1/auth/accounts/sensuser/sensitive-attributes",
            json={"attributes": SENSITIVE},
            headers=admin_headers,
        )
        assert put.status_code == 200
        assert put.json()["attributes"] == SENSITIVE

        get = client.get(
            "/api/v1/auth/accounts/sensuser/sensitive-attributes",
            headers=admin_headers,
        )
        assert get.status_code == 200
        assert get.json()["attributes"] == SENSITIVE

    def test_sensitive_get_missing_account_returns_404(self, client, admin_headers):
        resp = client.get(
            "/api/v1/auth/accounts/ghost/sensitive-attributes",
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_sensitive_requires_scope(self, client, viewer_headers):
        resp = client.get(
            "/api/v1/auth/accounts/admin/sensitive-attributes",
            headers=viewer_headers,
        )
        assert resp.status_code == 403


class TestGroupAttributes:
    """/api/v1/auth/groups/{name}/attributes"""

    def _make_group(self, client, admin_headers, name: str) -> None:
        client.post(
            "/api/v1/auth/groups",
            json={"name": name, "roles": [], "description": "", "members": []},
            headers=admin_headers,
        )

    def test_put_then_get_roundtrip(self, client, admin_headers):
        self._make_group(client, admin_headers, "attrgroup")
        put = client.put(
            "/api/v1/auth/groups/attrgroup/attributes",
            json={"attributes": NESTED},
            headers=admin_headers,
        )
        assert put.status_code == 200
        assert put.json()["attributes"] == NESTED

        get = client.get(
            "/api/v1/auth/groups/attrgroup/attributes",
            headers=admin_headers,
        )
        assert get.status_code == 200
        assert get.json()["attributes"] == NESTED

    def test_get_missing_group_returns_404(self, client, admin_headers):
        resp = client.get(
            "/api/v1/auth/groups/ghostgroup/attributes",
            headers=admin_headers,
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_put_missing_group_returns_404(self, client, admin_headers):
        resp = client.put(
            "/api/v1/auth/groups/ghostgroup/attributes",
            json={"attributes": {"x": 1}},
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_get_requires_scope(self, client, admin_headers, viewer_headers):
        self._make_group(client, admin_headers, "scopegroup")
        resp = client.get(
            "/api/v1/auth/groups/scopegroup/attributes",
            headers=viewer_headers,
        )
        assert resp.status_code == 403

    def test_sensitive_put_then_get_roundtrip(self, client, admin_headers):
        self._make_group(client, admin_headers, "sensgroup")
        put = client.put(
            "/api/v1/auth/groups/sensgroup/sensitive-attributes",
            json={"attributes": SENSITIVE},
            headers=admin_headers,
        )
        assert put.status_code == 200
        assert put.json()["attributes"] == SENSITIVE

        get = client.get(
            "/api/v1/auth/groups/sensgroup/sensitive-attributes",
            headers=admin_headers,
        )
        assert get.status_code == 200
        assert get.json()["attributes"] == SENSITIVE

    def test_sensitive_get_missing_group_returns_404(self, client, admin_headers):
        resp = client.get(
            "/api/v1/auth/groups/ghostgroup/sensitive-attributes",
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_sensitive_requires_scope(self, client, admin_headers, viewer_headers):
        self._make_group(client, admin_headers, "sensscopegroup")
        resp = client.get(
            "/api/v1/auth/groups/sensscopegroup/sensitive-attributes",
            headers=viewer_headers,
        )
        assert resp.status_code == 403
