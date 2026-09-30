"""Tests for auth router — login, me, permissions, 401/403."""

import secrets

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from scalo.metrics import create_metrics

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries, create_access_token
from dfe_engine.auth import hyperdx_role
from dfe_engine.auth.groups import GROUPS_SKIPPED
from dfe_engine.yaml_utils import yaml_dump, yaml_load


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


def _link(app, *names: str) -> None:
    """Link each group to what an IdP that sends group names asserts for it: its name."""
    for name in names:
        app.state.group_store.update(name, source_id=name)


class TestRolesFollowTheBoundAccount:
    """A session's store roles are those of the account it binds to, and no other's."""

    @pytest.mark.parametrize("held_by", ["account", "membership"])
    def test_a_token_whose_subject_is_anothers_stem_gets_none_of_their_roles(
        self, client: TestClient, app, api_settings, held_by
    ):
        """alice-smith-corp records Alice.Smith@corp, so a token for the bare stem binds nothing."""
        _link(app, "dfe-admins")
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
        _link(app, "dfe-analysts")
        client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "Alice.Smith@corp", "X-Oidc-Groups": "dfe-analysts"},
        )
        app.state.group_store.add_member("dfe-analysts", "alice-smith-corp")

        resp = client.get("/api/v1/auth/groups", headers=_bearer(api_settings, "alice-smith-corp"))

        assert resp.status_code == 200, resp.text
        assert resp.json()["items"] == []

    def test_a_proxied_user_with_no_local_account_gets_its_claim_groups(
        self, client: TestClient, app
    ):
        _link(app, "dfe-analysts")
        resp = client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "nia@example.com", "X-Oidc-Groups": "dfe-analysts"},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["roles"] == ["data_analyst"]

    def test_a_token_with_no_account_behind_it_gets_no_roles_from_its_claim(
        self, client: TestClient, app, api_settings
    ):
        """Every real login binds an account, so a claim alone is a token nothing vouches for."""
        headers = _bearer(api_settings, "omar@example.com", groups=["dfe-analysts"])

        resp = client.get("/api/v1/auth/me", headers=headers)

        assert app.state.account_store.get("omar-example-com") is None
        assert resp.status_code == 200, resp.text
        assert resp.json()["roles"] == []
        assert resp.json()["groups"] == []

    def test_a_deleted_accounts_live_token_is_refused(self, client: TestClient, app):
        password = secrets.token_urlsafe(16)
        app.state.account_store.create("leaver", password, groups=["dfe-admins"])
        app.state.group_store.add_member("dfe-admins", "leaver")
        login = client.post("/api/v1/auth/login", json={"username": "leaver", "password": password})
        assert "admin" in login.json()["roles"]
        app.state.group_store.remove_member("dfe-admins", "leaver")
        app.state.account_store.delete("leaver")

        resp = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        )

        assert resp.status_code == 401, resp.text
        assert resp.json()["code"] == "session_ended"

    def test_a_bound_idp_user_gets_its_accounts_roles(self, client: TestClient, app, api_settings):
        """Alice.Smith@corp binds alice-smith-corp through the stem; the claim is not consulted."""
        _link(app, "dfe-analysts")
        client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "Alice.Smith@corp", "X-Oidc-Groups": "dfe-analysts"},
        )

        resp = client.get(
            "/api/v1/auth/me",
            headers=_bearer(api_settings, "Alice.Smith@corp", groups=["dfe-admins"]),
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["roles"] == ["data_analyst"]

    def test_a_scim_adopted_local_login_whose_subject_differs_is_unbound(
        self, client: TestClient, app
    ):
        """Login answers with roles, but no account backs the session, so its token is refused."""
        password = secrets.token_urlsafe(16)
        store = app.state.account_store
        store.create("bob", password, groups=["dfe-analysts"])
        store.update("bob", source_provider="scim", subject="bob@corp")
        app.state.group_store.add_member("dfe-analysts", "bob")

        login = client.post("/api/v1/auth/login", json={"username": "bob", "password": password})
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        me = client.get("/api/v1/auth/me", headers=headers)
        refreshed = client.post("/api/v1/auth/refresh", headers=headers)

        assert login.status_code == 200, login.text
        assert login.json()["roles"] == ["data_analyst"]
        assert me.status_code == 401, me.text
        assert me.json()["code"] == "session_ended"
        assert refreshed.status_code == 401, refreshed.text

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
        # SCIM lists its members in the group, which is what a SCIM group grants by.
        app.state.group_store.add_member("dfe-analysts", "jane-doe")

        resp = client.get("/api/v1/auth/me", headers=_bearer(api_settings, "jane.doe"))

        assert resp.status_code == 200, resp.text
        assert resp.json()["roles"] == ["data_analyst"]

    def test_an_api_key_keeps_its_own_groups(self, client: TestClient, app):
        _, key = app.state.api_key_store.create("ci-analyst", groups=["dfe-analysts"])

        resp = client.get("/api/v1/auth/me", headers={"X-API-Key": key})

        assert resp.status_code == 200, resp.text
        assert resp.json()["roles"] == ["data_analyst"]

    def test_a_token_minted_for_an_api_key_gets_no_roles(
        self, client: TestClient, app, api_settings
    ):
        """An API-key subject binds no account; the key's groups belong to the key's own session."""
        app.state.api_key_store.create("ci-analyst", groups=["dfe-analysts"])
        headers = _bearer(api_settings, "apikey:ci-analyst", groups=["dfe-analysts"])

        resp = client.get("/api/v1/auth/me", headers=headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["roles"] == []

    @pytest.mark.parametrize("state", ["live", "revoked", "disabled", "expired"])
    def test_a_token_minted_for_an_api_key_holds_nothing_whatever_the_key(
        self, client: TestClient, app, api_settings, state
    ):
        """The key's own state never reaches the token: it binds no account, live or not."""
        store = app.state.api_key_store
        meta, _ = store.create("ci-admin", groups=["dfe-admins"])
        key_file = store._keys_dir / "ci-admin.yaml"
        if state == "revoked":
            store.revoke(meta.short_token)
        elif state == "disabled":
            yaml_dump({**yaml_load(key_file), "enabled": False}, key_file)
        elif state == "expired":
            yaml_dump({**yaml_load(key_file), "expires_at": "2020-01-01T00:00:00+00:00"}, key_file)
        headers = _bearer(
            api_settings,
            "apikey:ci-admin",
            roles=["admin"],
            groups=["dfe-admins"],
            org_ids=["acme"],
        )

        me = client.get("/api/v1/auth/me", headers=headers)
        keys = client.get("/api/v1/auth/api-keys", headers=headers)

        assert me.status_code == 200, me.text
        assert (me.json()["roles"], me.json()["groups"], me.json()["org_ids"]) == ([], [], [])
        assert keys.status_code == 403, keys.text

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param(
                "roles: [admin]\nmembers: [viewer]\nscope: org:../elsewhere/outside\n",
                id="bad-scope",
            ),
            pytest.param("roles: [admin\nmembers: [viewer]\n", id="malformed-yaml"),
            pytest.param("- admin\n- viewer\n", id="not-a-mapping"),
        ],
    )
    def test_an_unloadable_group_file_leaves_other_sessions_their_roles(
        self, client: TestClient, app, viewer_headers: dict, content
    ):
        """Every session lists the groups, so one unloadable file must not fail them all."""
        (app.state.group_store._dir / "climber.yaml").write_text(content, encoding="utf-8")

        resp = client.get("/api/v1/auth/me", headers=viewer_headers)

        assert resp.status_code == 200, resp.text
        assert "admin" not in resp.json()["roles"]
        assert "data_viewer" in resp.json()["roles"]

    def test_a_group_file_nested_past_the_parser_limit_leaves_sessions_and_logins_working(
        self, client: TestClient, app, viewer_headers: dict
    ):
        """The parser recurses per nesting level, so ~3000 levels raise RecursionError."""
        depth = 3000
        (app.state.group_store._dir / "climber.yaml").write_text(
            "roles: " + "[" * depth + "]" * depth + "\n", encoding="utf-8"
        )

        first = client.get("/api/v1/auth/me", headers=viewer_headers)
        second = client.get("/api/v1/auth/me", headers=viewer_headers)
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "operator", "password": "test-operator-pw"},
        )

        assert first.status_code == 200, first.text
        assert "data_viewer" in first.json()["roles"]
        assert second.status_code == 200, second.text
        assert login.status_code == 200, login.text
        assert sorted(login.json()["roles"]) == ["data_analyst", "infra_admin"]

    def test_creating_a_group_over_an_unloadable_file_is_a_conflict(
        self, client: TestClient, app, admin_headers: dict
    ):
        """The name is taken by a file that does not load, so the create must not replace it."""
        stored = app.state.group_store._dir / "climber.yaml"
        stored.write_text("roles: [admin]\nscope: org:../elsewhere/outside\n", encoding="utf-8")

        resp = client.post(
            "/api/v1/auth/groups", json={"name": "climber", "roles": []}, headers=admin_headers
        )

        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "conflict"
        assert "org:../elsewhere/outside" in stored.read_text(encoding="utf-8")

    def test_an_idp_user_added_to_a_group_by_hand_keeps_its_idp_groups(
        self, client: TestClient, app, admin_headers: dict, api_settings
    ):
        """jane's IdP asserts dfe-admins; an operator adding her to dfe-viewers adds, not replaces."""
        _link(app, "dfe-admins")
        signed_in = client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "jane@example.com", "X-Oidc-Groups": "dfe-admins"},
        )
        assert signed_in.status_code == 200, signed_in.text
        added = client.post(
            "/api/v1/auth/groups/dfe-viewers/members",
            json={"username": "jane-example-com"},
            headers=admin_headers,
        )
        assert added.status_code == 200, added.text

        resp = client.get("/api/v1/auth/me", headers=_bearer(api_settings, "jane@example.com"))

        assert resp.status_code == 200, resp.text
        assert "admin" in resp.json()["roles"]
        assert "data_viewer" in resp.json()["roles"]
        assert sorted(resp.json()["groups"]) == ["dfe-admins", "dfe-viewers"]


class TestATokenWhoseSubjectNamesAnAccountItDoesNotBind:
    """alice-smith-corp records Alice.Smith@corp, so a token for the bare name is not hers."""

    @pytest.fixture
    def laundered(self, client: TestClient, app, api_settings) -> dict[str, str]:
        """A token for the bare name whose claim carries Alice's groups, as a refresh once wrote."""
        _link(app, "dfe-admins")
        signed_in = client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "Alice.Smith@corp", "X-Oidc-Groups": "dfe-admins"},
        )
        assert signed_in.status_code == 200, signed_in.text
        return _bearer(api_settings, "alice-smith-corp", groups=["dfe-admins"])

    def test_its_claim_groups_give_it_no_roles(self, client: TestClient, laundered):
        resp = client.get("/api/v1/auth/me", headers=laundered)

        assert resp.status_code == 200, resp.text
        assert resp.json()["roles"] == []
        assert resp.json()["groups"] == []

    def test_it_cannot_refresh(self, client: TestClient, laundered):
        """Each refresh issues a new expiry, so a refreshable token here never expires."""
        resp = client.post("/api/v1/auth/refresh", headers=laundered)

        assert resp.status_code == 401, resp.text


def _listed(client: TestClient, headers: dict[str, str]) -> list[str]:
    resp = client.get("/api/v1/auth/groups", headers=headers)
    assert resp.status_code == 200, resp.text
    return [g["name"] for g in resp.json()["items"]]


class TestAMemberSeesTheGroupsItHolds:
    """No group:read, so only the groups the session's own roles come from are listed."""

    def test_a_proxied_idp_user_sees_its_idp_group(self, client: TestClient, app):
        _link(app, "dfe-analysts")
        headers = {"X-Oidc-Subject": "nia@example.com", "X-Oidc-Groups": "dfe-analysts"}

        assert _listed(client, headers) == ["dfe-analysts"]

    def test_a_bound_idp_token_sees_its_idp_group(self, client: TestClient, app, api_settings):
        _link(app, "dfe-analysts")
        client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "nia@example.com", "X-Oidc-Groups": "dfe-analysts"},
        )

        assert _listed(client, _bearer(api_settings, "nia@example.com")) == ["dfe-analysts"]

    def test_a_group_the_idp_names_by_its_provider_id_is_listed(self, client: TestClient, app):
        """Entra asserts object GUIDs, which the sync records as the group's source_id."""
        guid = "7b1d0f3e-0000-4000-8000-000000000001"
        app.state.group_store.create("entra-analysts", ["data_analyst"])
        app.state.group_store.update("entra-analysts", source_id=guid)
        headers = {"X-Oidc-Subject": "nia@example.com", "X-Oidc-Groups": guid}

        assert _listed(client, headers) == ["entra-analysts"]

    def test_an_api_key_sees_its_own_group(self, client: TestClient, app):
        _, key = app.state.api_key_store.create("ci-analyst", groups=["dfe-analysts"])

        assert _listed(client, {"X-API-Key": key}) == ["dfe-analysts"]

    def test_a_group_it_does_not_hold_stays_hidden(self, client: TestClient, app):
        _link(app, "dfe-analysts")
        headers = {"X-Oidc-Subject": "nia@example.com", "X-Oidc-Groups": "dfe-analysts"}

        resp = client.get("/api/v1/auth/groups/dfe-admins", headers=headers)

        assert resp.status_code == 404, resp.text

    def test_an_api_keys_group_named_as_anothers_provider_id_stays_hidden(
        self, client: TestClient, app
    ):
        """An API key's groups are names an operator chose, so the name decides first."""
        app.state.group_store.create("shadow-analysts", ["admin"])
        app.state.group_store.update("shadow-analysts", source_id="dfe-analysts")
        _, key = app.state.api_key_store.create("ci-analyst", groups=["dfe-analysts"])
        headers = {"X-API-Key": key}

        listed = _listed(client, headers)
        fetched = client.get("/api/v1/auth/groups/shadow-analysts", headers=headers)
        me = client.get("/api/v1/auth/me", headers=headers)

        assert listed == ["dfe-analysts"]
        assert fetched.status_code == 404, fetched.text
        assert me.json()["roles"] == ["data_analyst"]

    def test_an_idp_assertion_takes_the_group_linked_to_it_not_the_one_of_its_name(
        self, client: TestClient, app
    ):
        """A group of the asserted name is not the IdP's; the group linked to that id is."""
        app.state.group_store.create("linked-analysts", ["data_viewer"])
        app.state.group_store.update("linked-analysts", source_id="dfe-analysts")
        headers = {"X-Oidc-Subject": "nia@example.com", "X-Oidc-Groups": "dfe-analysts"}

        listed = _listed(client, headers)
        fetched = client.get("/api/v1/auth/groups/dfe-analysts", headers=headers)
        me = client.get("/api/v1/auth/me", headers=headers)

        assert listed == ["linked-analysts"]
        assert fetched.status_code == 404, fetched.text
        assert me.json()["roles"] == ["data_viewer"]


class TestOnlyASessionBoundToAnAccountRefreshes:
    """A refresh issues a new expiry, so what it refreshes must still be an account."""

    @pytest.mark.parametrize("revoked", [False, True], ids=["live-key", "revoked-key"])
    def test_a_token_minted_for_an_api_key_cannot_refresh(
        self, client: TestClient, app, api_settings, revoked
    ):
        meta, _ = app.state.api_key_store.create("ci-admin", groups=["dfe-admins"])
        if revoked:
            app.state.api_key_store.revoke(meta.short_token)
        headers = _bearer(api_settings, "apikey:ci-admin", groups=["dfe-admins"])

        resp = client.post("/api/v1/auth/refresh", headers=headers)

        assert resp.status_code == 401, resp.text

    def test_an_api_key_cannot_mint_a_token_by_refreshing(self, client: TestClient, app):
        _, key = app.state.api_key_store.create("ci-admin", groups=["dfe-admins"])

        resp = client.post("/api/v1/auth/refresh", headers={"X-API-Key": key})

        assert resp.status_code == 401, resp.text

    def test_a_deleted_accounts_token_cannot_refresh(self, client: TestClient, app):
        password = secrets.token_urlsafe(16)
        app.state.account_store.create("leaver", password, groups=["dfe-analysts"])
        login = client.post("/api/v1/auth/login", json={"username": "leaver", "password": password})
        assert login.status_code == 200, login.text
        app.state.account_store.delete("leaver")

        resp = client.post(
            "/api/v1/auth/refresh",
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        )

        assert resp.status_code == 401, resp.text

    def test_a_local_login_still_refreshes(self, client: TestClient):
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "operator", "password": "test-operator-pw"},
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        resp = client.post("/api/v1/auth/refresh", headers=headers)

        assert resp.status_code == 200, resp.text
        assert sorted(resp.json()["roles"]) == ["data_analyst", "infra_admin"]

    def test_a_bound_idp_user_still_refreshes(self, client: TestClient, app, api_settings):
        """Alice.Smith@corp binds alice-smith-corp through the stem, which records her."""
        _link(app, "dfe-analysts")
        client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "Alice.Smith@corp", "X-Oidc-Groups": "dfe-analysts"},
        )

        resp = client.post(
            "/api/v1/auth/refresh",
            headers=_bearer(api_settings, "Alice.Smith@corp", groups=["dfe-analysts"]),
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["roles"] == ["data_analyst"]

    def test_with_auth_disabled_a_refresh_is_not_refused(self, api_settings):
        """The dev posture's anonymous session has no account, and still refreshes."""
        settings = api_settings.model_copy(
            update={"env": "dev", "auth": api_settings.auth.model_copy(update={"enabled": False})}
        )
        app = create_app(settings=settings)
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                resp = client.post("/api/v1/auth/refresh")
        finally:
            _registries.clear()

        assert resp.status_code == 200, resp.text
        assert resp.json()["user_id"] == "dev"


class TestAMembershipTakenAwayIsGoneFromTheNextRequest:
    """An org's ClickHouse credential is handed out on org_ids, so a removed member loses it."""

    @pytest.fixture
    def orla(self, client: TestClient, app) -> dict[str, str]:
        """A local account in one org's group, and a token its refresh minted with that org."""
        password = secrets.token_urlsafe(16)
        app.state.account_store.create("orla", password, groups=["acme-analysts"])
        app.state.group_store.create(
            "acme-analysts", ["data_analyst"], members=["orla"], scope="org:acme"
        )
        login = client.post("/api/v1/auth/login", json={"username": "orla", "password": password})
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        refreshed = client.post("/api/v1/auth/refresh", headers=headers)
        assert refreshed.status_code == 200, refreshed.text
        assert _claims(refreshed.json()["access_token"])["org_ids"] == ["acme"]
        return {"Authorization": f"Bearer {refreshed.json()['access_token']}"}

    def test_a_refresh_after_removal_mints_no_org(
        self, client: TestClient, admin_headers: dict, orla
    ):
        removed = client.delete(
            "/api/v1/auth/groups/acme-analysts/members/orla", headers=admin_headers
        )
        assert removed.status_code == 200, removed.text

        refreshed = client.post("/api/v1/auth/refresh", headers=orla)
        assert refreshed.status_code == 200, refreshed.text
        fresh = {"Authorization": f"Bearer {refreshed.json()['access_token']}"}

        assert _claims(refreshed.json()["access_token"])["org_ids"] == []
        assert refreshed.json()["roles"] == []
        assert client.get("/api/v1/auth/me", headers=fresh).json()["org_ids"] == []
        assert client.get("/api/v1/auth/me", headers=orla).json()["org_ids"] == []

    def test_a_hand_edit_of_the_group_file_takes_the_org_away(self, client: TestClient, app, orla):
        """The account record still names the group; the group file decides."""
        group_file = app.state.group_store._dir / "acme-analysts.yaml"
        yaml_dump({**yaml_load(group_file), "members": []}, group_file)

        me = client.get("/api/v1/auth/me", headers=orla)
        refreshed = client.post("/api/v1/auth/refresh", headers=orla)

        assert app.state.account_store.get("orla").groups == ["acme-analysts"]
        assert me.status_code == 200, me.text
        assert (me.json()["roles"], me.json()["groups"], me.json()["org_ids"]) == ([], [], [])
        assert refreshed.status_code == 200, refreshed.text
        assert _claims(refreshed.json()["access_token"])["org_ids"] == []


def _sample(exposition: str, name: str, labels: dict[str, str]) -> float | None:
    for family in text_string_to_metric_families(exposition):
        for sample in family.samples:
            if sample.name == name and sample.labels == labels:
                return sample.value
    return None


def test_an_unloadable_group_file_is_counted_on_the_engines_metrics(api_settings):
    manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
    app = create_app(settings=api_settings, metrics_manager=manager)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            app.state.account_store.create(
                "counted", secrets.token_urlsafe(16), groups=["dfe-analysts"]
            )
            app.state.group_store.add_member("dfe-analysts", "counted")
            (app.state.group_store._dir / "climber.yaml").write_text(
                "roles: [admin\n", encoding="utf-8"
            )
            headers = _bearer(api_settings, "counted")
            first = client.get("/api/v1/auth/me", headers=headers)
            client.get("/api/v1/auth/me", headers=headers)
    finally:
        _registries.clear()

    assert first.status_code == 200, first.text
    assert first.json()["roles"] == ["data_analyst"]
    assert _sample(manager.metrics_text, GROUPS_SKIPPED, {"reason": "unreadable"}) == 1
