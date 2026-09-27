"""Tests for auth router — login, me, permissions, 401/403."""

import secrets

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.deps import create_access_token
from dfe_engine.auth import hyperdx_role


def _claims(token: str) -> dict:
    """Read a token the way a peer reads it, signature checked elsewhere."""
    return pyjwt.decode(token, options={"verify_signature": False})


class TestLogin:
    """POST /api/v1/auth/login"""

    def test_login_success(self, client: TestClient):
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "test-admin-pw"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["token_type"] == "bearer"
        assert data["access_token"]
        assert data["user_id"] == "admin"
        assert "admin" in data["roles"]
        assert data["expires_in"] == 30 * 60

    def test_login_bad_password(self, client: TestClient):
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "wrong"},
        )
        assert resp.status_code == 401
        data = resp.json()
        assert data["code"] == "unauthorized"

    def test_login_unknown_user(self, client: TestClient):
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": "nobody", "password": "test"},
        )
        assert resp.status_code == 401

    def test_login_all_roles(self, client: TestClient):
        """All three local accounts should authenticate."""
        for user, pw in [
            ("admin", "test-admin-pw"),
            ("operator", "test-operator-pw"),
            ("viewer", "test-viewer-pw"),
        ]:
            resp = client.post(
                "/api/v1/auth/login",
                json={"username": user, "password": pw},
            )
            assert resp.status_code == 200, f"Login failed for {user}"
            assert resp.json()["user_id"] == user


class TestRefresh:
    """POST /api/v1/auth/refresh"""

    def test_refresh_success(self, client: TestClient, admin_headers: dict):
        resp = client.post("/api/v1/auth/refresh", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["access_token"]
        assert data["user_id"] == "admin"

    def test_refresh_no_token(self, client: TestClient):
        resp = client.post("/api/v1/auth/refresh")
        assert resp.status_code == 401

    def test_refresh_rejects_disabled_account(self, client: TestClient, admin_headers: dict):
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "viewer", "password": "test-viewer-pw"},
        )
        assert login.status_code == 200
        viewer_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        disable = client.put(
            "/api/v1/auth/accounts/viewer",
            json={"enabled": False},
            headers=admin_headers,
        )
        assert disable.status_code == 200

        resp = client.post("/api/v1/auth/refresh", headers=viewer_headers)
        assert resp.status_code == 401
        assert resp.json()["message"] == "Account disabled"

        client.put(
            "/api/v1/auth/accounts/viewer",
            json={"enabled": True},
            headers=admin_headers,
        )


class TestMe:
    """GET /api/v1/auth/me"""

    def test_me_authenticated(self, client: TestClient, admin_headers: dict):
        resp = client.get("/api/v1/auth/me", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "admin"
        assert data["org_id"] == "test-org"
        assert "admin" in data["roles"]
        assert "dfe-admins" in data["groups"]
        assert data["external"] is False
        assert data["blocked"] is False
        assert data["disabled_at"] == ""
        assert data["blocked_at"] == ""

    def test_me_oidc_user_surfaces_external_true(self, client: TestClient, app, api_settings):
        from dfe_engine.api.deps import create_access_token

        store = app.state.account_store
        store.create("sso-user", "", groups=["dfe-viewers"])
        store.update("sso-user", external=True, source_provider="entra")
        token = create_access_token(
            data={"sub": "sso-user", "org_id": "test-org", "groups": ["dfe-viewers"]},
            settings=api_settings,
        )
        resp = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["external"] is True

    def test_me_no_token(self, client: TestClient):
        resp = client.get("/api/v1/auth/me")
        assert resp.status_code == 401

    def test_me_invalid_token(self, client: TestClient):
        resp = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer invalid-token-xyz"},
        )
        assert resp.status_code == 401

    def test_me_reflects_group_removal_without_relogin(
        self, client: TestClient, admin_headers: dict, api_settings
    ):
        from dfe_engine.api.deps import create_access_token

        client.post(
            "/api/v1/auth/groups",
            json={"name": "me-live-group", "roles": ["admin"], "members": ["viewer"]},
            headers=admin_headers,
        )
        token = create_access_token(
            data={
                "sub": "viewer",
                "org_id": "test-org",
                "roles": ["admin"],
                "groups": ["me-live-group", "dfe-admins"],
            },
            settings=api_settings,
        )
        headers = {"Authorization": f"Bearer {token}"}

        before = client.get("/api/v1/auth/me", headers=headers)
        assert "me-live-group" in before.json()["groups"]
        assert "admin" in before.json()["roles"]

        client.delete(
            "/api/v1/auth/groups/me-live-group/members/viewer",
            headers=admin_headers,
        )

        after = client.get("/api/v1/auth/me", headers=headers)
        assert after.status_code == 200
        data = after.json()
        assert "me-live-group" not in data["groups"]
        assert "admin" not in data["roles"]
        assert "data_viewer" in data["roles"]

    def test_me_rejects_disabled_account(self, client: TestClient, admin_headers: dict):
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "viewer", "password": "test-viewer-pw"},
        )
        assert login.status_code == 200
        viewer_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        client.put(
            "/api/v1/auth/accounts/viewer",
            json={"enabled": False},
            headers=admin_headers,
        )

        resp = client.get("/api/v1/auth/me", headers=viewer_headers)
        assert resp.status_code == 401
        assert resp.json()["message"] == "Account disabled"

        client.put(
            "/api/v1/auth/accounts/viewer",
            json={"enabled": True},
            headers=admin_headers,
        )

    def test_me_rejects_blocked_account(self, client: TestClient, admin_headers: dict):
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "viewer", "password": "test-viewer-pw"},
        )
        assert login.status_code == 200
        viewer_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        client.put(
            "/api/v1/auth/accounts/viewer",
            json={"blocked": True},
            headers=admin_headers,
        )

        resp = client.get("/api/v1/auth/me", headers=viewer_headers)
        assert resp.status_code == 401
        assert resp.json()["message"] == "Account blocked"

        client.put(
            "/api/v1/auth/accounts/viewer",
            json={"blocked": False},
            headers=admin_headers,
        )

    def test_me_rejects_disabled_external_jwt_by_sanitised_name(
        self, client: TestClient, app, api_settings
    ):
        from dfe_engine.api.deps import create_access_token

        store = app.state.account_store
        store.create("alice-example-com", "", groups=["dfe-viewers"])
        store.update("alice-example-com", external=True, source_provider="entra", enabled=False)
        token = create_access_token(
            data={"sub": "alice@example.com", "org_id": "test-org", "groups": ["dfe-viewers"]},
            settings=api_settings,
        )
        resp = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401
        assert resp.json()["message"] == "Account disabled"

    """GET /api/v1/auth/permissions"""

    def test_admin_has_wildcard(self, client: TestClient, admin_headers: dict):
        resp = client.get("/api/v1/auth/permissions", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "*" in data["permissions"]
        assert "admin" in data["roles"]

    def test_viewer_has_limited_perms(self, client: TestClient, viewer_headers: dict):
        resp = client.get("/api/v1/auth/permissions", headers=viewer_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "infra_viewer" in data["roles"] or "data_analyst_viewer" in data["roles"]
        assert "source:read" in data["permissions"]
        assert "config:write" not in data["permissions"]

    def test_permissions_ignore_stale_jwt_roles(self, client: TestClient, api_settings):
        """Roles in the JWT are not used; group membership is authoritative."""
        from dfe_engine.api.deps import create_access_token

        token = create_access_token(
            data={
                "sub": "viewer",
                "org_id": "test-org",
                "roles": ["admin"],
                "groups": ["dfe-admins"],
            },
            settings=api_settings,
        )
        resp = client.get(
            "/api/v1/auth/permissions",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "admin" not in data["roles"]
        assert "data_viewer" in data["roles"]
        assert "*" not in data["permissions"]

    def test_me_ignores_stale_jwt_when_account_has_no_groups(
        self, client: TestClient, admin_headers: dict, api_settings
    ):
        from dfe_engine.api.deps import create_access_token

        client.post(
            "/api/v1/auth/accounts",
            json={
                "username": "nogrp",
                "password": secrets.token_urlsafe(16),
                "email": "nogrp@example.com",
                "groups": [],
            },
            headers=admin_headers,
        )
        token = create_access_token(
            data={
                "sub": "nogrp",
                "org_id": "test-org",
                "roles": ["admin"],
                "groups": ["dfe-admins"],
            },
            settings=api_settings,
        )
        resp = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["groups"] == []
        assert data["roles"] == []
        assert "admin" not in data["permissions"]
        assert "*" not in data.get("permissions", [])


class TestTheHyperdxRoleClaim:
    """Every minted token says whether the account may change what a team sees."""

    def test_an_admin_login_carries_a_role_the_fork_allows(self, client: TestClient):
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "test-admin-pw"},
        )
        assert resp.status_code == 200
        claim = _claims(resp.json()["access_token"])[hyperdx_role.CLAIM]
        assert claim == hyperdx_role.TEAM_ADMIN
        assert claim in hyperdx_role.FORK_ACCEPTS

    def test_a_read_only_login_carries_a_role_the_fork_refuses(self, client: TestClient):
        resp = client.post(
            "/api/v1/auth/login",
            json={"username": "viewer", "password": "test-viewer-pw"},
        )
        assert resp.status_code == 200
        claim = _claims(resp.json()["access_token"])[hyperdx_role.CLAIM]
        assert claim == hyperdx_role.MEMBER
        assert claim not in hyperdx_role.FORK_ACCEPTS

    def test_a_refresh_resolves_the_claim_again_rather_than_copying_it(
        self, client: TestClient, viewer_headers: dict
    ):
        # The viewer's own groups carry no team-admin role, so the refreshed
        # token refuses whatever the presented one claimed.
        resp = client.post("/api/v1/auth/refresh", headers=viewer_headers)
        assert resp.status_code == 200
        assert _claims(resp.json()["access_token"])[hyperdx_role.CLAIM] == hyperdx_role.MEMBER

    def test_an_org_scoped_admin_carries_a_role_the_fork_refuses(self, client: TestClient, app):
        # Its admin role binds at one org's scope, and the fork's admin changes
        # what every team sees.
        password = secrets.token_urlsafe(16)
        app.state.account_store.create("acme-lead", password, groups=["acme-admins"])
        app.state.group_store.create(
            "acme-admins", ["admin"], members=["acme-lead"], scope="org:acme"
        )
        login = client.post(
            "/api/v1/auth/login", json={"username": "acme-lead", "password": password}
        )
        assert login.status_code == 200, login.text
        assert _claims(login.json()["access_token"])[hyperdx_role.CLAIM] == hyperdx_role.MEMBER

        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        refreshed = client.post("/api/v1/auth/refresh", headers=headers)
        assert refreshed.status_code == 200, refreshed.text
        claim = _claims(refreshed.json()["access_token"])[hyperdx_role.CLAIM]
        assert claim == hyperdx_role.MEMBER


def _bearer(api_settings, sub: str, **claims) -> dict[str, str]:
    """Headers for an engine token with *sub* and whatever claims the test names."""
    token = create_access_token(
        data={"sub": sub, "org_id": "test-org", **claims}, settings=api_settings
    )
    return {"Authorization": f"Bearer {token}"}


class TestRolesFollowTheBoundAccount:
    """A session's store roles are those of the account it binds to, and no other's."""

    @pytest.mark.parametrize("held_by", ["account", "membership"])
    def test_a_token_whose_subject_is_anothers_stem_gets_none_of_their_roles(
        self, client: TestClient, app, api_settings, held_by
    ):
        """alice-smith-corp records Alice.Smith@corp, so a token for the bare stem binds nothing."""
        signed_in = client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "Alice.Smith@corp", "X-Oidc-Groups": "dfe-admins"},
        )
        assert signed_in.status_code == 200, signed_in.text
        if held_by == "membership":
            app.state.account_store.update("alice-smith-corp", groups=[])
            app.state.group_store.add_member("dfe-admins", "alice-smith-corp")

        resp = client.get("/api/v1/auth/me", headers=_bearer(api_settings, "alice-smith-corp"))

        assert resp.status_code == 200, resp.text
        assert resp.json()["roles"] == []
        assert resp.json()["groups"] == []

    def test_a_token_whose_subject_is_anothers_stem_sees_none_of_their_groups(
        self, client: TestClient, app, api_settings
    ):
        """Members see their own groups, and the bare stem is not a member of Alice's."""
        client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "Alice.Smith@corp", "X-Oidc-Groups": "dfe-analysts"},
        )
        app.state.group_store.add_member("dfe-analysts", "alice-smith-corp")

        resp = client.get("/api/v1/auth/groups", headers=_bearer(api_settings, "alice-smith-corp"))

        assert resp.status_code == 200, resp.text
        assert resp.json()["items"] == []

    def test_a_proxied_user_with_no_local_account_gets_its_claim_groups(self, client: TestClient):
        resp = client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "nia@example.com", "X-Oidc-Groups": "dfe-analysts"},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["roles"] == ["data_analyst"]

    def test_a_token_with_no_account_behind_it_gets_its_claim_groups(
        self, client: TestClient, app, api_settings
    ):
        headers = _bearer(api_settings, "omar@example.com", groups=["dfe-analysts"])

        resp = client.get("/api/v1/auth/me", headers=headers)

        assert app.state.account_store.get("omar-example-com") is None
        assert resp.status_code == 200, resp.text
        assert resp.json()["roles"] == ["data_analyst"]

    def test_a_local_login_gets_its_accounts_roles(self, client: TestClient):
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "operator", "password": "test-operator-pw"},
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        resp = client.get("/api/v1/auth/me", headers=headers)

        assert resp.status_code == 200, resp.text
        assert sorted(resp.json()["roles"]) == ["data_analyst", "infra_admin"]

    def test_a_scim_account_bound_by_stem_gets_its_roles(
        self, client: TestClient, app, api_settings
    ):
        """jane.doe stems onto the SCIM record jane-doe, which records her as its subject."""
        store = app.state.account_store
        store.create("jane-doe", secrets.token_urlsafe(16), groups=["dfe-analysts"])
        store.update("jane-doe", source_provider="scim", subject="jane.doe")

        resp = client.get("/api/v1/auth/me", headers=_bearer(api_settings, "jane.doe"))

        assert resp.status_code == 200, resp.text
        assert resp.json()["roles"] == ["data_analyst"]

    def test_an_api_key_keeps_its_own_groups(self, client: TestClient, app):
        _, key = app.state.api_key_store.create("ci-analyst", groups=["dfe-analysts"])

        resp = client.get("/api/v1/auth/me", headers={"X-API-Key": key})

        assert resp.status_code == 200, resp.text
        assert resp.json()["roles"] == ["data_analyst"]
