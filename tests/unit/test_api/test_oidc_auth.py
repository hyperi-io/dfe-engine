"""Tests for OIDC header authentication path in get_current_user()."""

from fastapi.testclient import TestClient

from dfe_engine.api.deps import create_access_token


class TestOidcAuthentication:
    """OIDC headers (X-Oidc-Subject, X-Oidc-Groups) authenticate without JWT."""

    def test_oidc_subject_authenticates(self, client: TestClient):
        """X-Oidc-Subject header alone is sufficient for authentication."""
        resp = client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "alice@example.com"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "alice@example.com"

    def test_oidc_groups_resolve_roles(self, client: TestClient, app):
        """Groups from X-Oidc-Groups resolve to roles via GroupStore."""
        # dfe-admins group was seeded by bootstrap with roles=["admin"]
        resp = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "alice@example.com",
                "X-Oidc-Groups": "dfe-admins",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "admin" in data["roles"]
        assert "dfe-admins" in data["groups"]

    def test_groups_resolve_by_source_id(self, client: TestClient, app):
        """A provider that sends opaque group ids (Entra GUIDs, Google keys)
        resolves against the source_id the sync stored on the group file, not
        just the group name.

        Without this, an Entra login - whose token carries object GUIDs, never
        names - matches no group file and the user gets zero roles.
        """
        group_store = app.state.group_store
        guid = "0295f72c-e3f8-4962-9183-f95ef939e3b8"
        # A synced group: friendly name on the file, provider GUID as source_id.
        group_store.create("entra-admins", roles=["admin"], description="synced")
        group_store.update("entra-admins", source_provider="entra", source_id=guid)

        resp = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "grace@example.com",
                # The token carries the GUID, not "entra-admins".
                "X-Oidc-Groups": guid,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "admin" in data["roles"]

    def test_source_id_resolution_does_not_shadow_names(self, client: TestClient, app):
        """The source_id fallback only fires when a name misses - a plain name
        still resolves the ordinary way and is unaffected by the index."""
        group_store = app.state.group_store
        group_store.create("okta-viewers", roles=["data_viewer"], description="synced")
        group_store.update("okta-viewers", source_provider="okta", source_id="00g-xyz")

        resp = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "heidi@example.com",
                # dfe-admins matches by NAME; okta-viewers by NAME too (not id).
                "X-Oidc-Groups": "dfe-admins, okta-viewers",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "admin" in data["roles"]
        assert "data_viewer" in data["roles"]

    def test_the_proxy_path_stamps_the_configured_provider(self, client: TestClient, app):
        """The stamp is a provider name, not the protocol -- ``auth.proxy_provider``."""
        app.state.settings.auth.proxy_provider = "entra"

        resp = client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "ivan@example.com", "X-Oidc-Groups": "dfe-admins"},
        )

        assert resp.status_code == 200
        stamped = app.state.account_store.get("ivan-example-com")
        assert stamped.external is True
        assert stamped.source_provider == "entra"

    def test_a_dual_path_deployment_reconciles_one_identity(self, client: TestClient, app):
        """The defect this fixes: the proxy stamped 'oidc' and the RP callback
        asserted the provider name, so the guard read one IdP as two identities."""
        app.state.settings.auth.proxy_provider = "entra"
        client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "judy@example.com", "X-Oidc-Groups": "dfe-admins"},
        )

        # What oidc_callback does for provider "entra" on the same subject.
        account = app.state.jit_provisioner.ensure_account(
            "judy@example.com", ["dfe-analysts"], "entra", email="judy@example.com"
        )

        assert account.groups == ["dfe-analysts"]
        assert account.source_provider == "entra"

    def test_the_default_stamp_keeps_a_proxy_only_deployment_working(self, client: TestClient, app):
        """Unset, the stamp stays what deployed accounts already carry."""
        assert app.state.settings.auth.proxy_provider == "oidc"

        resp = client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "ken@example.com", "X-Oidc-Groups": "dfe-admins"},
        )

        assert resp.status_code == 200
        assert app.state.account_store.get("ken-example-com").source_provider == "oidc"

    def test_unknown_groups_no_roles(self, client: TestClient):
        """Unknown groups authenticate but yield no roles."""
        resp = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "bob@example.com",
                "X-Oidc-Groups": "unknown-group-xyz",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "bob@example.com"
        assert data["roles"] == []
        assert "unknown-group-xyz" in data["groups"]

    def test_multiple_comma_separated_groups(self, client: TestClient, app):
        """Multiple groups are split on comma and all roles collected."""
        # dfe-admins has ["admin"], dfe-analysts has ["data_analyst"]
        resp = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "carol@example.com",
                "X-Oidc-Groups": "dfe-admins, dfe-analysts",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "admin" in data["roles"]
        assert "data_analyst" in data["roles"]
        assert "dfe-admins" in data["groups"]
        assert "dfe-analysts" in data["groups"]

    def test_empty_groups_header(self, client: TestClient):
        """Empty X-Oidc-Groups header authenticates with no roles."""
        resp = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "dave@example.com",
                "X-Oidc-Groups": "",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "dave@example.com"
        assert data["roles"] == []

    def test_oidc_takes_precedence_over_bearer(self, client: TestClient, api_settings, app):
        """OIDC headers take precedence even when a valid Bearer token is present."""
        # Create a JWT with different identity
        jwt_token = create_access_token(
            data={"sub": "jwt-user", "org_id": "jwt-org", "roles": ["admin"]},
            settings=api_settings,
        )
        resp = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "oidc-user@example.com",
                "Authorization": f"Bearer {jwt_token}",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        # OIDC wins — user_id from OIDC, not JWT
        assert data["user_id"] == "oidc-user@example.com"

    def test_a_disabled_account_refuses_a_token_for_its_name_that_another_subject_holds(
        self, client: TestClient, api_settings, app
    ):
        """Roles resolve from the account stored under the token's subject, so its disable bites."""
        client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "Alice.Smith@corp", "X-Oidc-Groups": "dfe-admins"},
        )
        app.state.account_store.update("alice-smith-corp", enabled=False)
        token = create_access_token(
            data={"sub": "alice-smith-corp", "org_id": "test-org"}, settings=api_settings
        )

        resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

        assert resp.status_code == 401, resp.text
        assert resp.json()["message"] == "Account disabled"

    def test_oidc_whitespace_in_groups_stripped(self, client: TestClient, app):
        """Whitespace around group names is stripped."""
        resp = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "eve@example.com",
                "X-Oidc-Groups": " dfe-admins , dfe-viewers ",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "admin" in data["roles"]
        assert "data_viewer" in data["roles"]

    def test_oidc_headers_ignored_when_proxy_untrusted(self, api_settings):
        """Fail closed: with trust_proxy_auth_headers off, X-Oidc-* are NOT
        trusted (they are client-spoofable) - the request is unauthenticated.

        Regression guard for the header-spoofing auth bypass: a caller reaching
        the pod directly (bypassing Envoy) must not authenticate as an admin by
        setting X-Oidc-Subject / X-Oidc-Groups.
        """
        from dfe_engine.api.app import create_app
        from dfe_engine.api.deps import _registries

        untrusted = api_settings.model_copy(
            update={
                "auth": api_settings.auth.model_copy(update={"trust_proxy_auth_headers": False})
            }
        )
        application = create_app(settings=untrusted)
        try:
            with TestClient(application, raise_server_exceptions=False) as c:
                resp = c.get(
                    "/api/v1/auth/me",
                    headers={
                        "X-Oidc-Subject": "attacker@evil.example",
                        "X-Oidc-Groups": "dfe-admins",
                    },
                )
                assert resp.status_code == 401
        finally:
            _registries.clear()

    def test_a_proxy_header_asserting_the_local_admin_is_refused(self, client: TestClient, app):
        """dfe-engine#419 on the trusted-proxy path: 401, and the admin untouched."""
        store = app.state.account_store
        before = store.get("admin")
        assert before is not None

        resp = client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "admin", "X-Oidc-Groups": "dfe-viewers"},
        )

        assert resp.status_code == 401
        admin = store.get("admin")
        assert admin.groups == before.groups
        assert admin.updated_at == before.updated_at
        assert admin.external is False

    def test_disabled_external_account_is_refused(self, client: TestClient, app):
        store = app.state.account_store
        store.create("blocked-sso-example-com", "", groups=["dfe-viewers"])
        store.update(
            "blocked-sso-example-com",
            external=True,
            source_provider="oidc",
            enabled=False,
        )

        resp = client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "blocked-sso@example.com", "X-Oidc-Groups": "dfe-viewers"},
        )

        assert resp.status_code == 401
        assert resp.json()["message"] == "Account disabled"
        assert store.get("blocked-sso-example-com").last_login_at == ""

    def test_blocked_external_account_is_refused(self, client: TestClient, app):
        store = app.state.account_store
        store.create("locked-sso-example-com", "", groups=["dfe-viewers"])
        store.update(
            "locked-sso-example-com",
            blocked=True,
            external=True,
            source_provider="oidc",
        )

        resp = client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "locked-sso@example.com", "X-Oidc-Groups": "dfe-viewers"},
        )

        assert resp.status_code == 401
        assert resp.json()["message"] == "Account blocked"
        assert store.get("locked-sso-example-com").last_login_at == ""
