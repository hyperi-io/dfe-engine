"""Tests for OIDC header authentication path in get_current_user()."""

import shutil

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.deps import create_access_token
from dfe_engine.auth.groups import GroupStore
from dfe_engine.auth.jit import JitProvisioner
from tests.support.failing_stores import StampFailingAccountStore


def _link(app, *names: str) -> None:
    """Link each group to what an IdP that sends group names asserts for it: its name."""
    for name in names:
        app.state.group_store.update(name, source_id=name)


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
        _link(app, "dfe-admins")
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
        app.state.settings.auth.proxy_provider = "entra"

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

    def test_a_group_name_the_idp_asserts_takes_no_stored_group(self, client: TestClient, app):
        """A directory user can name a group dfe-admins; only the id the directory assigns links."""
        group_store = app.state.group_store
        group_store.create("okta-viewers", roles=["data_viewer"], description="synced")
        group_store.update("okta-viewers", source_provider="okta", source_id="00g-xyz")

        resp = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "heidi@example.com",
                "X-Oidc-Groups": "dfe-admins, okta-viewers",
            },
        )

        assert resp.status_code == 200
        assert resp.json()["roles"] == []
        assert resp.json()["groups"] == []

    def test_a_group_linked_to_another_provider_is_not_taken(self, client: TestClient, app):
        """Two IdPs: one cannot assert the other's group id to take its group."""
        group_store = app.state.group_store
        group_store.create("okta-admins", roles=["admin"], description="synced")
        group_store.update("okta-admins", source_provider="okta", source_id="00g-admins")
        app.state.settings.auth.proxy_provider = "entra"

        resp = client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "ivy@example.com", "X-Oidc-Groups": "00g-admins"},
        )

        assert resp.status_code == 200
        assert resp.json()["roles"] == []

    def test_a_group_linked_by_its_id_alone_answers_any_provider(self, client: TestClient, app):
        """A group file an operator linked carries only source_id, as the sync fills the rest."""
        app.state.group_store.update("dfe-analysts", source_id="00g-analysts")

        resp = client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "jon@example.com", "X-Oidc-Groups": "00g-analysts"},
        )

        assert resp.status_code == 200
        assert resp.json()["roles"] == ["data_analyst"]
        assert resp.json()["groups"] == ["dfe-analysts"]

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

    def test_a_group_name_that_is_a_path_grants_nothing(self, client: TestClient, app):
        """The proxy's group names are joined onto the group directory, so one must not climb out."""
        elsewhere = app.state.group_store._dir.parent / "elsewhere"
        GroupStore(elsewhere).create("outside", roles=["admin"])

        resp = client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "lena@example.com", "X-Oidc-Groups": "../elsewhere/outside"},
        )

        assert resp.status_code == 200
        assert resp.json()["roles"] == []

    def test_the_proxied_caller_reads_their_own_account(self, client: TestClient):
        """Authentication binds an email-shaped subject to its JIT stem, and so must /me."""
        resp = client.get(
            "/api/v1/auth/accounts/me",
            headers={"X-Oidc-Subject": "mia@example.com", "X-Oidc-Groups": "dfe-viewers"},
        )

        assert resp.status_code == 200
        assert resp.json()["username"] == "mia-example-com"

    def test_the_proxied_caller_updates_their_own_contact_fields(self, client: TestClient, app):
        resp = client.put(
            "/api/v1/auth/accounts/me",
            headers={"X-Oidc-Subject": "noah@example.com", "X-Oidc-Groups": "dfe-viewers"},
            json={"phone": "+61 2 5550 1234"},
        )

        assert resp.status_code == 200
        assert app.state.account_store.get("noah-example-com").phone == "+61 2 5550 1234"

    def test_the_proxied_caller_is_told_their_password_lives_at_the_idp(self, client: TestClient):
        resp = client.post(
            "/api/v1/auth/accounts/reset-password",
            headers={"X-Oidc-Subject": "olga@example.com", "X-Oidc-Groups": "dfe-viewers"},
            json={"current_password": "anything", "new_password": "a-long-enough-password-1"},
        )

        assert resp.status_code == 409
        assert resp.json()["code"] == "external_account"

    def test_unknown_groups_no_roles(self, client: TestClient):
        """Unknown groups authenticate but yield no roles and hold no group."""
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
        assert data["groups"] == []

    def test_multiple_comma_separated_groups(self, client: TestClient, app):
        """Multiple groups are split on comma and all roles collected."""
        # dfe-admins has ["admin"], dfe-analysts has ["data_analyst"]
        _link(app, "dfe-admins", "dfe-analysts")
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
        _link(app, "dfe-admins", "dfe-viewers")
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


class TestAJitFailureRefusesTheLogin:
    """A proxied session with no account behind it could not be disabled locally."""

    @pytest.mark.parametrize("subject", ["@@@", "a" * 129], ids=["empty-stem", "too-long"])
    def test_a_subject_that_names_no_account_is_refused(
        self, client: TestClient, audit_events, subject
    ):
        resp = client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": subject, "X-Oidc-Groups": "dfe-admins"},
        )

        assert resp.status_code == 401, resp.text
        assert resp.json()["code"] == "unauthorized"
        denied = [e for e in audit_events if e["event"] == "auth.login.denied"]
        assert [(e["user_id"], e["reason"]) for e in denied] == [(subject, "unusable_subject")]
        assert not [e for e in audit_events if e["event"] == "auth.jit.provision_failed"]

    def test_a_login_the_account_store_cannot_record_is_refused(
        self, client: TestClient, app, audit_events
    ):
        accounts_dir = app.state.account_store._dir
        shutil.rmtree(accounts_dir)
        accounts_dir.write_text("")

        resp = client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "kim@example.com", "X-Oidc-Groups": "dfe-admins"},
        )

        assert resp.status_code == 503, resp.text
        assert resp.json()["code"] == "service_unavailable"
        failed = [e for e in audit_events if e["event"] == "auth.jit.provision_failed"]
        assert [e["user_id"] for e in failed] == ["kim@example.com"]
        assert not [e for e in audit_events if e["event"] == "auth.login.denied"]

    def test_a_login_whose_account_cannot_be_stamped_is_a_503_and_the_retry_signs_in(
        self, client: TestClient, app, audit_events
    ):
        """An unstamped account would read as a local one and refuse every later login."""
        _link(app, "dfe-admins")
        healthy = app.state.jit_provisioner
        app.state.jit_provisioner = JitProvisioner(
            account_store=StampFailingAccountStore(app.state.account_store._dir),
            group_store=app.state.group_store,
        )
        headers = {"X-Oidc-Subject": "kim@example.com", "X-Oidc-Groups": "dfe-admins"}

        failed = client.get("/api/v1/auth/me", headers=headers)
        app.state.jit_provisioner = healthy
        retried = client.get("/api/v1/auth/me", headers=headers)

        assert failed.status_code == 503, failed.text
        assert failed.json()["code"] == "service_unavailable"
        assert retried.status_code == 200, retried.text
        assert retried.json()["roles"] == ["admin"]
        assert not [e for e in audit_events if e["event"] == "auth.login.denied"]
