"""Tests for OIDC header authentication path in get_current_user()."""

import json
import shutil

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.deps import create_access_token
from dfe_engine.auth.groups import GroupStore
from dfe_engine.auth.jit import JitProvisioner
from dfe_engine.settings import DFESettings
from tests.support.failing_stores import StampFailingAccountStore
from tests.unit.test_auth.factories import make_oidc_provider


def _link(app, *names: str) -> None:
    """Link each group, for the gateway's provider, to what an IdP sending names asserts: its name."""
    provider = app.state.settings.auth.proxy_provider
    for name in names:
        app.state.group_store.update(name, source_provider=provider, source_id=name)


class CountingGroupStore(GroupStore):
    """The YAML group store, counting the listings and lookups made of it."""

    def __init__(self, groups_dir) -> None:
        super().__init__(groups_dir)
        self.lists = 0
        self.gets = 0

    def list(self):
        self.lists += 1
        return super().list()

    def get(self, name):
        self.gets += 1
        return super().get(name)


def _assert_refused_without_role(resp, app, stem: str) -> None:
    """The proxied identity holds no role, so it is refused and no account is made for it."""
    assert resp.status_code == 401, resp.text
    assert resp.json()["code"] == "no_role_granted"
    assert app.state.account_store.get(stem) is None


class TestOidcAuthentication:
    """OIDC headers (X-Oidc-Subject, X-Oidc-Groups) authenticate without JWT."""

    def test_oidc_subject_authenticates(self, client: TestClient, app):
        """The proxy's headers authenticate with no JWT."""
        _link(app, "dfe-viewers")
        resp = client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "alice@example.com", "X-Oidc-Groups": "dfe-viewers"},
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

    def test_a_manual_proxy_provider_ignores_the_asserted_groups(self, client: TestClient, app):
        # Membership for a manual provider lives in the engine, so the gateway's groups header grants nothing.
        from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider

        _link(app, "dfe-admins")
        app.state.oidc_provider_registry.create(
            "oidc",
            OIDCProvider(
                type="generic",
                issuer="https://sso.example.com",
                groups=GroupResolutionConfig(mode="manual"),
            ),
        )
        headers = {"X-Oidc-Subject": "mia@example.com", "X-Oidc-Groups": "dfe-admins"}

        refused = client.get("/api/v1/auth/me", headers=headers)
        _assert_refused_without_role(refused, app, "mia-example-com")
        app.state.group_store.add_member("dfe-viewers", "mia-example-com")
        admitted = client.get("/api/v1/auth/me", headers=headers)

        assert admitted.status_code == 200, admitted.text
        viewers = sorted(app.state.group_store.get("dfe-viewers").roles)
        assert "admin" not in viewers
        assert (admitted.json()["roles"], admitted.json()["groups"]) == (viewers, ["dfe-viewers"])

    def test_a_hand_added_member_holds_the_groups_roles_through_the_gateway(
        self, client: TestClient, app
    ):
        """A gateway that forwards no groups claim reaches roles only through hand membership."""
        app.state.group_store.add_member("dfe-analysts", "pat-example-com")

        resp = client.get("/api/v1/auth/me", headers={"X-Oidc-Subject": "pat@example.com"})

        assert resp.status_code == 200, resp.text
        assert (resp.json()["roles"], resp.json()["groups"]) == (
            ["data_analyst"],
            ["dfe-analysts"],
        )

    def test_the_gateway_and_a_token_resolve_one_subject_the_same(
        self, client: TestClient, api_settings, app
    ):
        """Hand membership and a linked id both count, on either path."""
        _link(app, "dfe-infra")
        app.state.group_store.add_member("dfe-analysts", "quinn-example-com")
        proxied = client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "quinn@example.com", "X-Oidc-Groups": "dfe-infra"},
        )
        token = create_access_token(data={"sub": "quinn@example.com"}, settings=api_settings)

        bearer = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

        assert proxied.status_code == 200, proxied.text
        assert bearer.status_code == 200, bearer.text
        for session in (proxied.json(), bearer.json()):
            assert session["roles"] == ["data_analyst", "infra_admin"]
            assert session["groups"] == ["dfe-analysts", "dfe-infra"]

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

        _assert_refused_without_role(resp, app, "heidi-example-com")

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

        _assert_refused_without_role(resp, app, "ivy-example-com")

    @pytest.mark.parametrize("provider", ["okta", "google", "corp-sso"])
    def test_a_group_file_linked_by_its_id_alone_answers_no_provider(
        self, client: TestClient, app, provider
    ):
        """A link that names no provider would answer every provider, so the file loads unlinked."""
        stored = app.state.group_store._dir / "id-only-admins.yaml"
        stored.write_text("roles: [admin]\nsource_id: Admins\n", encoding="utf-8")
        app.state.settings.auth.proxy_provider = provider

        resp = client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "jon@example.com", "X-Oidc-Groups": "Admins"},
        )

        _assert_refused_without_role(resp, app, "jon-example-com")

    def test_the_proxy_path_stamps_the_configured_provider(self, client: TestClient, app):
        """The stamp is a provider name, not the protocol -- ``auth.proxy_provider``."""
        app.state.settings.auth.proxy_provider = "entra"
        _link(app, "dfe-admins")

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
        _link(app, "dfe-admins", "dfe-analysts")
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
        _link(app, "dfe-admins")
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

        _assert_refused_without_role(resp, app, "lena-example-com")

    def test_the_proxied_caller_reads_their_own_account(self, client: TestClient, app):
        """Authentication binds an email-shaped subject to its JIT stem, and so must /me."""
        _link(app, "dfe-viewers")
        resp = client.get(
            "/api/v1/auth/accounts/me",
            headers={"X-Oidc-Subject": "mia@example.com", "X-Oidc-Groups": "dfe-viewers"},
        )

        assert resp.status_code == 200
        assert resp.json()["username"] == "mia-example-com"

    def test_the_proxied_caller_updates_their_own_contact_fields(self, client: TestClient, app):
        _link(app, "dfe-viewers")
        resp = client.put(
            "/api/v1/auth/accounts/me",
            headers={"X-Oidc-Subject": "noah@example.com", "X-Oidc-Groups": "dfe-viewers"},
            json={"phone": "+61 2 5550 1234"},
        )

        assert resp.status_code == 200
        assert app.state.account_store.get("noah-example-com").phone == "+61 2 5550 1234"

    def test_the_proxied_caller_is_told_their_password_lives_at_the_idp(
        self, client: TestClient, app
    ):
        _link(app, "dfe-viewers")
        resp = client.post(
            "/api/v1/auth/accounts/reset-password",
            headers={"X-Oidc-Subject": "olga@example.com", "X-Oidc-Groups": "dfe-viewers"},
            json={"current_password": "anything", "new_password": "a-long-enough-password-1"},
        )

        assert resp.status_code == 409
        assert resp.json()["code"] == "external_account"

    def test_unknown_groups_are_refused(self, client: TestClient, app, audit_events):
        """Groups that link nothing hold no role, so the login is refused with what fixes it."""
        resp = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "bob@example.com",
                "X-Oidc-Groups": "unknown-group-xyz",
            },
        )

        _assert_refused_without_role(resp, app, "bob-example-com")
        assert resp.json()["message"] == (
            "OIDC login refused: no DFE group with a role is linked to your identity provider "
            "groups. Ask a DFE administrator to link one."
        )
        refused = [e for e in audit_events if e["event"] == "auth.jit.login_refused"]
        denied = [e for e in audit_events if e["event"] == "auth.login.denied"]
        assert [(e["user_id"], e["source_provider"], e["reason"]) for e in refused] == [
            ("bob@example.com", "oidc", "no_role_granted")
        ]
        assert [(e["user_id"], e["auth_path"], e["reason"]) for e in denied] == [
            ("bob@example.com", "oidc", "no_role_granted")
        ]
        assert "unknown-group-xyz" not in json.dumps(refused + denied, default=str)

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

    def test_an_empty_groups_header_is_refused(self, client: TestClient, app):
        resp = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "dave@example.com",
                "X-Oidc-Groups": "",
            },
        )

        _assert_refused_without_role(resp, app, "dave-example-com")

    def test_oidc_takes_precedence_over_bearer(self, client: TestClient, api_settings, app):
        """OIDC headers take precedence even when a valid Bearer token is present."""
        _link(app, "dfe-viewers")
        # Create a JWT with different identity
        jwt_token = create_access_token(
            data={"sub": "jwt-user", "org_id": "jwt-org", "roles": ["admin"]},
            settings=api_settings,
        )
        resp = client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "oidc-user@example.com",
                "X-Oidc-Groups": "dfe-viewers",
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
        _link(app, "dfe-admins")
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


class TestOneRequestReadsTheGroupsOnce:
    """Each listing reads every group file, so a request lists the groups once and looks none up."""

    @pytest.fixture
    def counting(self, app, client) -> CountingGroupStore:
        """The app's group store and its JIT provisioner, both reading through one counting store."""
        store = CountingGroupStore(app.state.group_store._dir)
        app.state.group_store = store
        app.state.jit_provisioner = JitProvisioner(
            account_store=app.state.account_store,
            group_store=store,
            source_provider_bindings=app.state.settings.auth.source_provider_bindings,
        )
        return store

    @pytest.mark.parametrize("returning", [False, True], ids=["first-sign-in", "returning"])
    def test_a_gateway_request(self, client: TestClient, app, counting, returning):
        _link(app, "dfe-admins")
        headers = {"X-Oidc-Subject": "ruth@example.com", "X-Oidc-Groups": "dfe-admins"}
        if returning:
            assert client.get("/api/v1/auth/permissions", headers=headers).status_code == 200
        counting.lists = counting.gets = 0

        resp = client.get("/api/v1/auth/permissions", headers=headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["roles"] == ["admin"]
        assert (counting.lists, counting.gets) == (1, 0)

    def test_a_token_request(self, client: TestClient, counting, admin_headers):
        counting.lists = counting.gets = 0

        resp = client.get("/api/v1/auth/permissions", headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["roles"] == ["admin"]
        assert (counting.lists, counting.gets) == (1, 0)


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
        _link(app, "dfe-admins")
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
        # The class of the failure, not its text.
        assert [e["error_type"] for e in failed] == ["AccountExistsError"]
        assert "error" not in failed[0]
        assert not [e for e in audit_events if e["event"] == "auth.login.denied"]
        # No line, the operational one included, carries the exception or the store path it names.
        logged = json.dumps(audit_events, default=str)
        assert "Account already exists" not in logged
        assert str(accounts_dir) not in logged

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


class TestAGroupLinkedThroughTheGroupsApi:
    """Okta, dex and Keycloak send group names, so a group whose source ID is the name it is sent takes its roles at login."""

    IDP = {"X-Oidc-Subject": "jane@corp.example", "X-Oidc-Groups": "dfe-viewers"}
    VIEWER_ROLES = ["data_analyst_viewer", "data_viewer", "infra_viewer"]

    def test_a_created_group_grants_its_roles_on_the_proxy_and_bound_account_paths(
        self, admin_headers: dict[str, str], api_settings: DFESettings, client: TestClient
    ):
        created = client.post(
            "/api/v1/auth/groups",
            headers=admin_headers,
            json={
                "name": "okta-soc",
                "roles": ["data_analyst"],
                "source_id": "SOC Analysts",
                "source_provider": "oidc",
            },
        )
        proxied = client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "jane@corp.example", "X-Oidc-Groups": "SOC Analysts"},
        )
        token = create_access_token(data={"sub": "jane@corp.example"}, settings=api_settings)
        bound = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

        assert created.status_code == 201, created.text
        assert proxied.status_code == 200, proxied.text
        assert (proxied.json()["roles"], proxied.json()["groups"]) == (
            ["data_analyst"],
            ["okta-soc"],
        )
        assert bound.status_code == 200, bound.text
        assert (bound.json()["roles"], bound.json()["groups"]) == (["data_analyst"], ["okta-soc"])

    def test_a_seeded_group_given_its_own_name_grants_its_roles(
        self, admin_headers: dict[str, str], client: TestClient
    ):
        linked = client.put(
            "/api/v1/auth/groups/dfe-viewers",
            headers=admin_headers,
            json={"source_id": "dfe-viewers", "source_provider": "oidc"},
        )
        resp = client.get("/api/v1/auth/me", headers=self.IDP)

        assert linked.status_code == 200, linked.text
        assert resp.status_code == 200, resp.text
        assert (resp.json()["roles"], resp.json()["groups"]) == (
            self.VIEWER_ROLES,
            ["dfe-viewers"],
        )

    def test_the_seeded_group_without_a_source_id_grants_nothing(self, app, client: TestClient):
        resp = client.get("/api/v1/auth/me", headers=self.IDP)

        _assert_refused_without_role(app=app, resp=resp, stem="jane-corp-example")

    def test_a_token_claim_login_jit_records_takes_the_roles(
        self, admin_headers: dict[str, str], api_settings: DFESettings, app, client: TestClient
    ):
        # ensure_account is what the relying-party callback runs with the groups claim, so only the IdP exchange is left out.
        app.state.oidc_provider_registry.create(name="okta", provider=make_oidc_provider())
        created = client.post(
            "/api/v1/auth/groups",
            headers=admin_headers,
            json={
                "name": "okta-soc",
                "roles": ["data_analyst"],
                "source_id": "SOC Analysts",
                "source_provider": "okta",
            },
        )
        app.state.jit_provisioner.ensure_account(
            email="kate@corp.example",
            oidc_groups=["SOC Analysts"],
            source_provider="okta",
            user_id="kate@corp.example",
        )
        token = create_access_token(data={"sub": "kate@corp.example"}, settings=api_settings)

        resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

        assert created.status_code == 201, created.text
        assert resp.status_code == 200, resp.text
        assert (resp.json()["roles"], resp.json()["groups"]) == (["data_analyst"], ["okta-soc"])

    def test_a_login_through_another_provider_takes_nothing(
        self, admin_headers: dict[str, str], app, client: TestClient
    ):
        app.state.oidc_provider_registry.create(name="okta", provider=make_oidc_provider())
        created = client.post(
            "/api/v1/auth/groups",
            headers=admin_headers,
            json={
                "name": "okta-soc",
                "roles": ["data_analyst"],
                "source_id": "SOC Analysts",
                "source_provider": "okta",
            },
        )

        resp = client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "lee@corp.example", "X-Oidc-Groups": "SOC Analysts"},
        )

        assert created.status_code == 201, created.text
        _assert_refused_without_role(app=app, resp=resp, stem="lee-corp-example")
