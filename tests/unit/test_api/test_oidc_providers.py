#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_oidc_providers.py
#  Purpose:      Tests for OIDC provider CRUD REST endpoints
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for POST/GET/PUT/DELETE /api/v1/auth/oidc-providers endpoints."""

import json
from pathlib import Path

import pytest


def _provider_yaml(api_settings, name: str) -> Path:
    """The YAML file the registry persists a provider to."""
    return Path(api_settings.auth.auth_dir) / "oidc-providers" / f"{name}.yaml"


def _field_errors(response) -> list[str]:
    """The field names a 422 named, so a form can point at the offending input."""
    return [err["field"] for err in response.json()["errors"]]


def _create_provider(client, admin_headers, name="test-provider", **overrides):
    """Helper to create a provider and return the response."""
    body = {
        "name": name,
        "type": "generic",
        "display_name": "Test Provider",
        "issuer": "https://accounts.example.com",
        "client_id_env": "OIDC_CLIENT_ID",
    }
    body.update(overrides)
    return client.post("/api/v1/auth/oidc-providers", json=body, headers=admin_headers)


class TestCreateProvider:
    """POST /api/v1/auth/oidc-providers"""

    def test_create_provider(self, client, admin_headers):
        resp = _create_provider(client, admin_headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "test-provider"
        assert data["type"] == "generic"
        assert data["enabled"] is True
        assert data["display_name"] == "Test Provider"
        assert data["issuer"] == "https://accounts.example.com"
        assert data["client_id_env"] == "OIDC_CLIENT_ID"
        assert data["groups"]["mode"] == "token_claim"
        assert data["created_at"] != ""

    def test_create_with_groups_config(self, client, admin_headers):
        resp = _create_provider(
            client,
            admin_headers,
            name="api-provider",
            type="entra_id",
            groups={
                "mode": "api",
                "sync_interval": 1800,
                "tenant_id": "contoso-tenant",
                "tenant_id_env": "ENTRA_TENANT_ID",
                "client_secret_env": "ENTRA_CLIENT_SECRET",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["groups"]["mode"] == "api"
        assert data["groups"]["sync_interval"] == 1800
        assert data["groups"]["tenant_id"] == "contoso-tenant"
        assert data["groups"]["tenant_id_env"] == "ENTRA_TENANT_ID"

    def test_create_honours_enabled(self, client, app, admin_headers):
        resp = _create_provider(client, admin_headers, name="off-at-birth", enabled=False)
        assert resp.status_code == 201
        assert resp.json()["enabled"] is False
        assert not app.state.oidc_rp.has_provider("off-at-birth")

    def test_create_sets_rp_client_secret_env(self, client, admin_headers):
        """The RP client_secret_env round-trips - without it a provider created
        via the API could never complete a login (no secret for the exchange)."""
        resp = _create_provider(
            client,
            admin_headers,
            name="rp-secret",
            client_secret_env="OIDC_RP_SECRET",
        )
        assert resp.status_code == 201
        assert resp.json()["client_secret_env"] == "OIDC_RP_SECRET"
        got = client.get("/api/v1/auth/oidc-providers/rp-secret", headers=admin_headers)
        assert got.json()["client_secret_env"] == "OIDC_RP_SECRET"

    def test_create_duplicate_returns_409(self, client, admin_headers):
        _create_provider(client, admin_headers, name="dup-provider")
        resp = _create_provider(client, admin_headers, name="dup-provider")
        assert resp.status_code == 409
        assert resp.json()["code"] == "conflict"

    def test_create_requires_admin(self, client, viewer_headers):
        resp = client.post(
            "/api/v1/auth/oidc-providers",
            json={"name": "blocked", "type": "generic"},
            headers=viewer_headers,
        )
        assert resp.status_code == 403

    def test_create_missing_fields_returns_422(self, client, admin_headers):
        resp = client.post("/api/v1/auth/oidc-providers", json={}, headers=admin_headers)
        assert resp.status_code == 422


class TestListProviders:
    """GET /api/v1/auth/oidc-providers"""

    def test_list_empty(self, client, admin_headers):
        resp = client.get("/api/v1/auth/oidc-providers", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["items"] == []

    def test_list_after_create(self, client, admin_headers):
        _create_provider(client, admin_headers, name="prov-a")
        _create_provider(client, admin_headers, name="prov-b")
        resp = client.get("/api/v1/auth/oidc-providers", headers=admin_headers)
        assert resp.status_code == 200
        names = [p["name"] for p in resp.json()["items"]]
        assert "prov-a" in names
        assert "prov-b" in names

    def test_list_requires_admin(self, client, viewer_headers):
        resp = client.get("/api/v1/auth/oidc-providers", headers=viewer_headers)
        assert resp.status_code == 403


class TestGetProvider:
    """GET /api/v1/auth/oidc-providers/{name}"""

    def test_get_provider(self, client, admin_headers):
        _create_provider(client, admin_headers, name="get-test")
        resp = client.get("/api/v1/auth/oidc-providers/get-test", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "get-test"
        assert data["type"] == "generic"

    def test_get_nonexistent_returns_404(self, client, admin_headers):
        resp = client.get("/api/v1/auth/oidc-providers/nonexistent", headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_get_requires_admin(self, client, viewer_headers):
        resp = client.get("/api/v1/auth/oidc-providers/anything", headers=viewer_headers)
        assert resp.status_code == 403

    def test_env_var_names_visible_not_values(self, client, admin_headers):
        """Env var names are exposed, not the actual secret values."""
        _create_provider(
            client,
            admin_headers,
            name="secret-check",
            client_id_env="MY_CLIENT_ID",
        )
        resp = client.get("/api/v1/auth/oidc-providers/secret-check", headers=admin_headers)
        data = resp.json()
        # The env var NAME is visible
        assert data["client_id_env"] == "MY_CLIENT_ID"
        # The response should not contain any resolved env var values
        # (no "password", "secret", "token" fields with actual values)


class TestUpdateProvider:
    """PUT /api/v1/auth/oidc-providers/{name}"""

    def test_update_enabled(self, client, admin_headers):
        _create_provider(client, admin_headers, name="upd-test")
        resp = client.put(
            "/api/v1/auth/oidc-providers/upd-test",
            json={"enabled": False},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    def test_update_display_name(self, client, admin_headers):
        _create_provider(client, admin_headers, name="upd-name")
        resp = client.put(
            "/api/v1/auth/oidc-providers/upd-name",
            json={"display_name": "New Name"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "New Name"

    def test_update_groups_config(self, client, admin_headers):
        _create_provider(client, admin_headers, name="upd-groups", type="okta")
        resp = client.put(
            "/api/v1/auth/oidc-providers/upd-groups",
            json={
                "groups": {
                    "api_token_env": "OKTA_API_TOKEN",
                    "mode": "api",
                    "okta_domain": "acme.okta.com",
                    "sync_interval": 900,
                }
            },
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["groups"]["mode"] == "api"
        assert resp.json()["groups"]["sync_interval"] == 900

    def test_update_nonexistent_returns_404(self, client, admin_headers):
        resp = client.put(
            "/api/v1/auth/oidc-providers/ghost",
            json={"enabled": False},
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_update_requires_admin(self, client, viewer_headers):
        resp = client.put(
            "/api/v1/auth/oidc-providers/anything",
            json={"enabled": False},
            headers=viewer_headers,
        )
        assert resp.status_code == 403


class TestDeleteProvider:
    """DELETE /api/v1/auth/oidc-providers/{name}"""

    def test_delete_provider(self, client, admin_headers):
        _create_provider(client, admin_headers, name="del-test")
        resp = client.delete("/api/v1/auth/oidc-providers/del-test", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] == "del-test"
        assert data["orphaned_groups"] == []
        assert data["disabled_accounts"] == []

        # Confirm deleted
        resp = client.get("/api/v1/auth/oidc-providers/del-test", headers=admin_headers)
        assert resp.status_code == 404

    def test_delete_reports_orphaned_groups(self, client, admin_headers, app):
        """Groups with source_provider matching the deleted provider are reported."""
        _create_provider(client, admin_headers, name="orphan-prov")

        # Create groups that reference this provider via the store directly
        group_store = app.state.group_store
        group_store.create("synced-group-1", roles=["data_viewer"], description="Synced")
        group_store.update("synced-group-1", source_provider="orphan-prov", source_id="g1")
        group_store.add_member("synced-group-1", "alice")

        group_store.create("synced-group-2", roles=["admin"], description="Synced 2")
        group_store.update("synced-group-2", source_provider="orphan-prov", source_id="g2")

        resp = client.delete("/api/v1/auth/oidc-providers/orphan-prov", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] == "orphan-prov"
        assert len(data["orphaned_groups"]) == 2

        orphan_names = [g["name"] for g in data["orphaned_groups"]]
        assert "synced-group-1" in orphan_names
        assert "synced-group-2" in orphan_names

        # Check member count is reported
        g1 = next(g for g in data["orphaned_groups"] if g["name"] == "synced-group-1")
        assert g1["member_count"] == 1
        assert g1["roles"] == ["data_viewer"]

    def test_delete_soft_disables_accounts_tied_to_the_provider(self, client, admin_headers, app):
        _create_provider(client, admin_headers, name="gone-idp")
        store = app.state.account_store
        store.create("jit-user", "")
        store.update(
            "jit-user",
            external=True,
            source_provider="gone-idp",
            oidc_id="gone-idp",
            subject="jit@corp.com",
            email="jit@corp.com",
            name="Jit User",
        )
        store.create("scim-user", "provisioned-Pw-1")
        store.update(
            "scim-user",
            source_provider="scim",
            oidc_id="gone-idp",
            subject="scim@corp.com",
        )
        store.create("other-idp-user", "")
        store.update(
            "other-idp-user",
            external=True,
            source_provider="other-idp",
            oidc_id="other-idp",
        )
        store.create("already-off", "")
        store.update(
            "already-off",
            enabled=False,
            source_provider="gone-idp",
            oidc_id="gone-idp",
        )

        resp = client.delete("/api/v1/auth/oidc-providers/gone-idp", headers=admin_headers)
        assert resp.status_code == 200
        rows = resp.json()["disabled_accounts"]
        by_previous = {row["previous_username"]: row for row in rows}
        assert set(by_previous) == {"jit-user", "scim-user"}
        for previous, row in by_previous.items():
            assert row["username"].endswith("-deleted")
            assert row["username"] != previous
            assert store.get(previous) is None
            anonymized = store.get(row["username"])
            assert anonymized is not None
            assert anonymized.enabled is False
            assert anonymized.disabled_at != ""
            assert anonymized.previous_username == previous
            assert anonymized.email == ""
            assert anonymized.name == ""
            assert anonymized.subject == ""

        assert by_previous["jit-user"]["source_provider"] == "gone-idp"
        assert by_previous["scim-user"]["oidc_id"] == "gone-idp"
        assert store.get("other-idp-user").enabled is True
        assert store.get("already-off").enabled is False

    def test_delete_nonexistent_returns_404(self, client, admin_headers):
        resp = client.delete("/api/v1/auth/oidc-providers/nonexistent", headers=admin_headers)
        assert resp.status_code == 404

    def test_delete_requires_admin(self, client, viewer_headers):
        resp = client.delete("/api/v1/auth/oidc-providers/anything", headers=viewer_headers)
        assert resp.status_code == 403


class TestSyncProvider:
    """POST /api/v1/auth/oidc-providers/{name}/sync"""

    def test_sync_generic_provider(self, client, admin_headers):
        """Generic adapter returns empty — sync reports skipped (mode is token_claim)."""
        _create_provider(client, admin_headers, name="sync-test")
        resp = client.post("/api/v1/auth/oidc-providers/sync-test/sync", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        # A provider outside api mode gets skipped
        assert data["total"] == 0

    def test_a_skipped_group_is_reported(self, client, app, admin_headers, tmp_path, monkeypatch):
        """The directory's second group sanitises to no valid group name."""
        from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider

        directory = tmp_path / "directory.json"
        directory.write_text(
            json.dumps(
                {
                    "groups": [
                        {"id": "g-ops", "name": "operators"},
                        {"id": "g-bad", "name": "!!!"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("DFE_TEST_SYNC_DIRECTORY", str(directory))
        app.state.oidc_provider_registry.create(
            "mock-dir",
            OIDCProvider(
                type="generic",
                enabled=True,
                issuer="https://sso.example.com",
                groups=GroupResolutionConfig(
                    mode="api",
                    directory_backend="mock",
                    mock_directory_env="DFE_TEST_SYNC_DIRECTORY",
                ),
            ),
        )

        resp = client.post("/api/v1/auth/oidc-providers/mock-dir/sync", headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert (resp.json()["created"], resp.json()["groups_skipped"]) == (1, 1)

    def test_an_idp_group_cannot_take_the_seeded_admins_by_name(
        self, client, app, admin_headers, tmp_path, monkeypatch
    ):
        """A directory group whose display name slugs to dfe-admins gets no link and no role."""
        from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider

        attacker = "attacker-guid-123"
        directory = tmp_path / "directory.json"
        group = {"id": attacker, "name": "DFE Admins", "email": ""}
        directory.write_text(json.dumps({"groups": [group]}), encoding="utf-8")
        monkeypatch.setenv("DFE_TEST_SYNC_DIRECTORY", str(directory))
        app.state.oidc_provider_registry.create(
            "mock-dir",
            OIDCProvider(
                type="entra_id",
                enabled=True,
                issuer="https://login.example.com/tenant/v2.0",
                groups=GroupResolutionConfig(
                    mode="api",
                    directory_backend="mock",
                    mock_directory_env="DFE_TEST_SYNC_DIRECTORY",
                ),
            ),
        )
        before = app.state.group_store.get("dfe-admins")

        resp = client.post("/api/v1/auth/oidc-providers/mock-dir/sync", headers=admin_headers)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert (body["created"], body["updated"], body["groups_skipped"]) == (0, 0, 1)
        assert app.state.group_store.get("dfe-admins") == before
        me = client.get(
            "/api/v1/auth/me",
            headers={"X-Oidc-Subject": "mallory@example.com", "X-Oidc-Groups": attacker},
        )
        assert me.status_code == 200, me.text
        assert "admin" not in me.json()["roles"]
        assert "dfe-admins" not in me.json()["groups"]

    def test_the_interval_sync_runs_beside_the_api(self, app, client):
        # It waits a tick before its first run, so it never races the explicit syncs in these tests.
        assert app.state.oidc_group_sync.done() is False

    def test_sync_nonexistent_returns_404(self, client, admin_headers):
        resp = client.post("/api/v1/auth/oidc-providers/ghost/sync", headers=admin_headers)
        assert resp.status_code == 404

    def test_sync_requires_admin(self, client, viewer_headers):
        resp = client.post("/api/v1/auth/oidc-providers/anything/sync", headers=viewer_headers)
        assert resp.status_code == 403


class TestTestProvider:
    """GET /api/v1/auth/oidc-providers/{name}/test"""

    def test_test_generic_provider(self, client, admin_headers):
        """Generic adapter always returns success."""
        _create_provider(client, admin_headers, name="conn-test")
        resp = client.get("/api/v1/auth/oidc-providers/conn-test/test", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "Generic provider" in data["message"]

    def test_test_nonexistent_returns_404(self, client, admin_headers):
        resp = client.get("/api/v1/auth/oidc-providers/ghost/test", headers=admin_headers)
        assert resp.status_code == 404

    def test_test_requires_admin(self, client, viewer_headers):
        resp = client.get("/api/v1/auth/oidc-providers/anything/test", headers=viewer_headers)
        assert resp.status_code == 403


class TestVerifyLoginConfig:
    """GET /api/v1/auth/oidc-providers/{name}/verify-login"""

    def test_reports_missing_client_id(self, client, admin_headers):
        """An unset client_id env var is flagged, not silently passed."""
        _create_provider(client, admin_headers, name="vl-missing", client_id_env="OIDC_UNSET_XYZ")
        resp = client.get(
            "/api/v1/auth/oidc-providers/vl-missing/verify-login", headers=admin_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        client_id = next(c for c in data["checks"] if c["name"] == "client_id")
        assert client_id["ok"] is False

    def test_reports_literal_client_id_as_misconfiguration(self, client, app, admin_headers):
        """YAML that stores the client id value instead of an env var name is flagged.

        The API refuses that shape now, so the only way in is a file written out
        of band (a gitops sync, an operator editing the YAML).
        """
        from dfe_engine.auth.oidc.models import OIDCProvider

        app.state.oidc_provider_registry.create(
            "vl-literal-id",
            OIDCProvider(
                type="generic",
                issuer="https://accounts.example.com",
                client_id_env="0oa15mxzztuHEzwr7698",
            ),
        )
        resp = client.get(
            "/api/v1/auth/oidc-providers/vl-literal-id/verify-login", headers=admin_headers
        )
        assert resp.status_code == 200
        client_id = next(c for c in resp.json()["checks"] if c["name"] == "client_id")
        assert client_id["ok"] is False
        assert "environment variable name" in client_id["detail"]
        assert "0oa" not in client_id["detail"]

    def test_reports_a_stored_client_secret_as_present(self, client, admin_headers):
        """A secret written through the API satisfies the check without any env var."""
        _create_provider(client, admin_headers, name="vl-stored", client_secret="rp-secret-value")
        resp = client.get(
            "/api/v1/auth/oidc-providers/vl-stored/verify-login", headers=admin_headers
        )
        assert resp.status_code == 200
        check = next(c for c in resp.json()["checks"] if c["name"] == "client_secret")
        assert check["ok"] is True
        assert "rp-secret-value" not in resp.text

    def test_reports_present_client_id(self, client, admin_headers, monkeypatch):
        """A resolvable client_id env var passes its check (value never returned)."""
        monkeypatch.setenv("OIDC_PRESENT_ID", "some-client-id")
        _create_provider(client, admin_headers, name="vl-present", client_id_env="OIDC_PRESENT_ID")
        resp = client.get(
            "/api/v1/auth/oidc-providers/vl-present/verify-login", headers=admin_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        client_id = next(c for c in data["checks"] if c["name"] == "client_id")
        assert client_id["ok"] is True
        # The secret value must never appear in the response.
        assert "some-client-id" not in resp.text

    def test_discovery_failure_is_reported_not_raised(self, client, admin_headers):
        """An unreachable/invalid discovery URL yields ok=False, not a 500."""
        _create_provider(
            client, admin_headers, name="vl-baddisco", issuer="https://accounts.example.com"
        )
        resp = client.get(
            "/api/v1/auth/oidc-providers/vl-baddisco/verify-login", headers=admin_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        discovery = next(c for c in data["checks"] if c["name"] == "discovery")
        assert discovery["ok"] is False

    def test_verify_login_nonexistent_returns_404(self, client, admin_headers):
        resp = client.get("/api/v1/auth/oidc-providers/ghost/verify-login", headers=admin_headers)
        assert resp.status_code == 404

    def test_verify_login_requires_admin(self, client, viewer_headers):
        resp = client.get(
            "/api/v1/auth/oidc-providers/anything/verify-login", headers=viewer_headers
        )
        assert resp.status_code == 403


class TestRelyingPartyStaysInSync:
    """Every write to the registry takes effect on the login routes immediately.

    The relying party snapshots the enabled providers when it is built, so a
    write that does not rebuild it leaves ``/auth/oidc/{name}/login`` serving the
    provider set from process start until the engine restarts.
    """

    def test_create_makes_the_provider_loginable(self, client, app, admin_headers):
        assert not app.state.oidc_rp.has_provider("fresh")
        _create_provider(client, admin_headers, name="fresh")
        assert app.state.oidc_rp.has_provider("fresh")

    def test_created_credentials_reach_the_rp_without_a_restart(self, client, app, admin_headers):
        """The whole point of dfe-engine#392: no env edit, no restart, just a login."""
        _create_provider(
            client,
            admin_headers,
            name="live",
            client_id="live-client",
            client_secret="live-secret",
        )
        registered = app.state.oidc_rp._oauth.create_client("live")
        assert registered.client_id == "live-client"
        assert registered.client_secret == "live-secret"

    def test_a_rotated_secret_reaches_the_rp_without_a_restart(self, client, app, admin_headers):
        _create_provider(client, admin_headers, name="rot", client_id="c", client_secret="first")
        client.put(
            "/api/v1/auth/oidc-providers/rot",
            json={"client_secret": "second"},
            headers=admin_headers,
        )
        assert app.state.oidc_rp._oauth.create_client("rot").client_secret == "second"

    def test_disable_stops_serving_logins(self, client, app, admin_headers):
        _create_provider(client, admin_headers, name="toggled")
        assert app.state.oidc_rp.has_provider("toggled")

        client.put(
            "/api/v1/auth/oidc-providers/toggled", json={"enabled": False}, headers=admin_headers
        )
        assert not app.state.oidc_rp.has_provider("toggled")

        client.put(
            "/api/v1/auth/oidc-providers/toggled", json={"enabled": True}, headers=admin_headers
        )
        assert app.state.oidc_rp.has_provider("toggled")

    def test_delete_stops_serving_logins(self, client, app, admin_headers):
        _create_provider(client, admin_headers, name="detached")
        assert app.state.oidc_rp.has_provider("detached")

        client.delete("/api/v1/auth/oidc-providers/detached", headers=admin_headers)
        assert not app.state.oidc_rp.has_provider("detached")

    def test_provider_with_no_issuer_does_not_break_the_write(self, client, app, admin_headers):
        """An unregisterable provider is skipped, and the others still register.

        The API refuses a provider with no issuer, so this one is written out of band.
        """
        from dfe_engine.auth.oidc.models import OIDCProvider

        app.state.oidc_provider_registry.create("no-issuer", OIDCProvider(type="generic"))
        resp = _create_provider(client, admin_headers, name="good")
        assert resp.status_code == 201
        assert app.state.oidc_rp.has_provider("good")
        assert not app.state.oidc_rp.has_provider("no-issuer")


class TestCredentialsGoToTheSecretStore:
    """A credential sent to the API is written to the store; config keeps a path.

    The old shape only accepted the NAME of an env var, so a provider created in
    a running engine had no credentials until someone edited the deployment env
    and restarted it.
    """

    def test_client_secret_lands_in_the_store_not_the_yaml(
        self, client, app, api_settings, admin_headers
    ):
        resp = _create_provider(
            client, admin_headers, name="stored", client_secret="rp-client-secret"
        )
        assert resp.status_code == 201
        assert resp.json()["client_secret_path"] == "oidc/stored/client_secret"
        assert app.state.dfe_secrets.get("oidc/stored/client_secret") == "rp-client-secret"
        assert "rp-client-secret" not in _provider_yaml(api_settings, "stored").read_text()

    def test_a_secret_is_never_echoed_back(self, client, admin_headers):
        resp = _create_provider(
            client, admin_headers, name="no-echo", client_secret="rp-client-secret"
        )
        assert "rp-client-secret" not in resp.text
        got = client.get("/api/v1/auth/oidc-providers/no-echo", headers=admin_headers)
        assert "rp-client-secret" not in got.text

    def test_client_id_stays_a_plain_field(self, client, api_settings, admin_headers):
        """The client id is not secret, so it is readable in config and in the API."""
        resp = _create_provider(client, admin_headers, name="plain-id", client_id="0oa15mxz")
        assert resp.json()["client_id"] == "0oa15mxz"
        assert "0oa15mxz" in _provider_yaml(api_settings, "plain-id").read_text()

    def test_group_api_secrets_land_in_the_store(self, client, app, api_settings, admin_headers):
        resp = _create_provider(
            client,
            admin_headers,
            name="okta-groups",
            type="okta",
            groups={"mode": "api", "okta_domain": "acme.okta.com", "api_token": "okta-ssws-token"},
        )
        assert resp.status_code == 201
        assert resp.json()["groups"]["api_token_path"] == "oidc/okta-groups/groups_api_token"
        assert app.state.dfe_secrets.get("oidc/okta-groups/groups_api_token") == "okta-ssws-token"
        assert "okta-ssws-token" not in _provider_yaml(api_settings, "okta-groups").read_text()

    def test_rotation_replaces_the_stored_secret(self, client, app, admin_headers):
        _create_provider(client, admin_headers, name="rotated", client_secret="first")
        resp = client.put(
            "/api/v1/auth/oidc-providers/rotated",
            json={"client_secret": "second"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert app.state.dfe_secrets.get("oidc/rotated/client_secret") == "second"

    def test_an_unrelated_update_keeps_the_stored_secret(self, client, app, admin_headers):
        """Flipping enabled must not strand the credential the provider logs in with."""
        _create_provider(client, admin_headers, name="kept", client_secret="keep-me")
        client.put(
            "/api/v1/auth/oidc-providers/kept", json={"enabled": False}, headers=admin_headers
        )
        resp = client.get("/api/v1/auth/oidc-providers/kept", headers=admin_headers)
        assert resp.json()["client_secret_path"] == "oidc/kept/client_secret"
        assert app.state.dfe_secrets.get("oidc/kept/client_secret") == "keep-me"

    def test_a_groups_edit_keeps_a_stored_directory_token(self, client, app, admin_headers):
        _create_provider(
            client,
            admin_headers,
            name="okta-edit",
            type="okta",
            groups={"mode": "api", "okta_domain": "acme.okta.com", "api_token": "okta-ssws-token"},
        )
        resp = client.put(
            "/api/v1/auth/oidc-providers/okta-edit",
            json={"groups": {"mode": "api", "okta_domain": "acme.okta.com", "sync_interval": 900}},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["groups"]["api_token_path"] == "oidc/okta-edit/groups_api_token"
        assert app.state.dfe_secrets.get("oidc/okta-edit/groups_api_token") == "okta-ssws-token"

    def test_detach_removes_the_stored_secrets(self, client, app, admin_headers):
        """Nothing is left in the store that can authenticate as a detached provider."""
        _create_provider(client, admin_headers, name="gone", client_secret="rp-client-secret")
        assert app.state.dfe_secrets.exists("oidc/gone/client_secret")

        client.delete("/api/v1/auth/oidc-providers/gone", headers=admin_headers)
        assert not app.state.dfe_secrets.exists("oidc/gone/client_secret")


# No service account and no admin: a Google provider logs in on the user's own token.
_GOOGLE = {"type": "google", "issuer": "https://accounts.google.com", "groups": {"mode": "api"}}
_CLOUD_IDENTITY_SCOPE = "https://www.googleapis.com/auth/cloud-identity.groups.readonly"


class TestScopes:
    """The scopes a provider asks its IdP for: a per-type default, or a validated override."""

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            (_GOOGLE, ["openid", "email", "profile", _CLOUD_IDENTITY_SCOPE]),
            ({"type": "entra_id"}, ["openid", "email", "profile"]),
            ({"type": "generic"}, ["openid", "email", "profile", "groups"]),
            ({"type": "okta"}, ["openid", "email", "profile", "groups"]),
        ],
        ids=["google", "entra_id", "generic", "okta"],
    )
    def test_create_uses_the_type_default(self, client, app, admin_headers, overrides, expected):
        resp = _create_provider(client, admin_headers, name="scoped", **overrides)
        assert resp.status_code == 201, resp.text
        assert resp.json()["scopes"] == expected
        stored = app.state.oidc_provider_registry.get("scoped")
        assert stored.scopes == " ".join(expected)

    def test_google_reaches_the_rp_without_a_groups_scope(self, client, app, admin_headers):
        _create_provider(client, admin_headers, name="gws", **_GOOGLE)
        registered = app.state.oidc_rp._oauth.create_client("gws")
        assert registered.client_kwargs["scope"] == f"openid email profile {_CLOUD_IDENTITY_SCOPE}"

    def test_create_takes_an_override(self, client, app, admin_headers):
        resp = _create_provider(
            client,
            admin_headers,
            name="custom",
            type="okta",
            scopes=["openid", "email", "offline_access", "email"],
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["scopes"] == ["openid", "email", "offline_access"]
        assert app.state.oidc_provider_registry.get("custom").scopes == (
            "openid email offline_access"
        )

    @pytest.mark.parametrize(
        ("scopes", "field"),
        [
            (["email", "profile"], "scopes"),
            ([], "scopes"),
            (["openid", "email profile"], "scopes"),
            (["openid", 'gr"oups'], "scopes"),
            (["openid", 5], "scopes.1"),
            ("email profile", "scopes"),
            ({"openid": True}, "scopes"),
        ],
        ids=[
            "no-openid",
            "empty",
            "two-scopes-in-one-entry",
            "quote",
            "not-a-string",
            "string-without-openid",
            "neither-a-list-nor-a-string",
        ],
    )
    def test_create_refuses_a_bad_override_and_writes_nothing(
        self, client, admin_headers, api_settings, scopes, field
    ):
        written = Path(api_settings.auth.auth_dir) / "oidc-providers"
        before = sorted(written.rglob("*"))
        resp = _create_provider(client, admin_headers, name="bad-scopes", scopes=scopes)
        assert resp.status_code == 422, resp.text
        assert _field_errors(resp) == [field]
        assert sorted(written.rglob("*")) == before

    def test_update_replaces_the_scopes(self, client, app, admin_headers):
        _create_provider(client, admin_headers, name="upd-scopes", **_GOOGLE)
        resp = client.put(
            "/api/v1/auth/oidc-providers/upd-scopes",
            json={"scopes": ["openid", "email"]},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["scopes"] == ["openid", "email"]
        assert app.state.oidc_provider_registry.get("upd-scopes").scopes == "openid email"
        registered = app.state.oidc_rp._oauth.create_client("upd-scopes")
        assert registered.client_kwargs["scope"] == "openid email"

    def test_an_update_without_scopes_keeps_them(self, client, app, admin_headers):
        _create_provider(client, admin_headers, name="keep-scopes", scopes=["openid", "email"])
        client.put(
            "/api/v1/auth/oidc-providers/keep-scopes",
            json={"display_name": "Renamed"},
            headers=admin_headers,
        )
        assert app.state.oidc_provider_registry.get("keep-scopes").scopes == "openid email"

    def test_a_space_separated_string_is_taken_as_the_scopes(self, client, app, admin_headers):
        resp = _create_provider(
            client,
            admin_headers,
            name="str-scopes",
            type="okta",
            scopes="openid  email offline_access",
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["scopes"] == ["openid", "email", "offline_access"]
        assert app.state.oidc_provider_registry.get("str-scopes").scopes == (
            "openid email offline_access"
        )

    def test_a_form_can_send_the_stored_scopes_string_back(self, client, app, admin_headers):
        # The setup status serves the provider model, whose scopes are one string.
        _create_provider(client, admin_headers, name="round-trip", scopes=["openid", "email"])
        stored = app.state.oidc_provider_registry.get("round-trip").scopes
        resp = client.put(
            "/api/v1/auth/oidc-providers/round-trip",
            json={"display_name": "Renamed", "scopes": stored},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["scopes"] == ["openid", "email"]

    def test_a_blank_string_is_no_override(self, client, app, admin_headers):
        resp = _create_provider(client, admin_headers, name="blank-scopes", scopes="  ")
        assert resp.status_code == 201, resp.text
        assert resp.json()["scopes"] == ["openid", "email", "profile", "groups"]
        client.put(
            "/api/v1/auth/oidc-providers/blank-scopes",
            json={"scopes": "openid email"},
            headers=admin_headers,
        )
        resp = client.put(
            "/api/v1/auth/oidc-providers/blank-scopes", json={"scopes": ""}, headers=admin_headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["scopes"] == ["openid", "email"]

    def test_update_refuses_scopes_without_openid(self, client, app, admin_headers):
        _create_provider(client, admin_headers, name="upd-bad-scopes")
        resp = client.put(
            "/api/v1/auth/oidc-providers/upd-bad-scopes",
            json={"scopes": ["email", "profile", "groups"]},
            headers=admin_headers,
        )
        assert resp.status_code == 422, resp.text
        assert _field_errors(resp) == ["scopes"]
        stored = app.state.oidc_provider_registry.get("upd-bad-scopes")
        assert stored.scopes == "openid email profile groups"


class TestEnvNameFieldsRejectPastedSecrets:
    """A ``*_env`` field takes the NAME of an env var; anything else is a 422.

    The form invited pasting the secret itself, and the API used to accept it and
    write the literal into the provider YAML.
    """

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("client_secret_env", "sUpEr-s3cret-value"),
            ("client_id_env", "0oa15mxzztuHEzwr7698"),
        ],
    )
    def test_create_rejects_a_literal(self, client, admin_headers, field, value):
        resp = _create_provider(client, admin_headers, name="bad-env", **{field: value})
        assert resp.status_code == 422
        assert _field_errors(resp) == [field]
        assert value not in resp.text

    def test_update_rejects_a_literal(self, client, admin_headers):
        _create_provider(client, admin_headers, name="upd-bad-env")
        resp = client.put(
            "/api/v1/auth/oidc-providers/upd-bad-env",
            json={"client_secret_env": "sUpEr-s3cret-value"},
            headers=admin_headers,
        )
        assert resp.status_code == 422
        assert _field_errors(resp) == ["client_secret_env"]

    def test_group_env_fields_reject_a_literal(self, client, admin_headers):
        resp = _create_provider(
            client,
            admin_headers,
            name="bad-group-env",
            groups={"mode": "api", "api_token_env": "00abcSSWStokenvalue-xyz"},
        )
        assert resp.status_code == 422
        assert _field_errors(resp) == ["groups.api_token_env"]

    def test_a_real_env_name_is_still_accepted(self, client, admin_headers):
        resp = _create_provider(
            client, admin_headers, name="good-env", client_secret_env="OIDC_RP_SECRET"
        )
        assert resp.status_code == 201
        assert resp.json()["client_secret_env"] == "OIDC_RP_SECRET"

    @pytest.mark.parametrize("name", ["../escape", "with/slash", "-leading-dash", ""])
    def test_a_name_that_is_not_a_safe_path_segment_is_rejected(self, client, admin_headers, name):
        """The name is a filename stem and a secret-store path segment."""
        resp = _create_provider(client, admin_headers, name=name)
        assert resp.status_code == 422

    @pytest.mark.parametrize("name", ["a" * 129, "okta\n"], ids=["too-long", "trailing-newline"])
    def test_a_name_the_registry_refuses_writes_no_secret(
        self, client, app, api_settings, admin_headers, name
    ):
        """Create stores the secret before the YAML, so the registry's rule must refuse first."""
        written = [
            Path(api_settings.secrets.path),
            Path(api_settings.auth.auth_dir) / "oidc-providers",
        ]
        before = [sorted(path.rglob("*")) for path in written]

        resp = _create_provider(client, admin_headers, name=name, client_secret="rp-client-secret")

        assert resp.status_code == 422, resp.text
        assert _field_errors(resp) == ["name"]
        assert [sorted(path.rglob("*")) for path in written] == before
        assert not app.state.dfe_secrets.exists(f"oidc/{name}/client_secret")


class TestProviderFieldRules:
    """Create and update refuse fields a provider's type and mode cannot use, before anything is written.

    The rules themselves are covered in test_auth/test_oidc/test_field_rules.py; these check the wiring.
    """

    @pytest.mark.parametrize(
        ("overrides", "fields"),
        [
            ({"groups": {"mode": "api"}}, ["groups.mode"]),
            (
                {"groups": {"api_token": "okta-ssws-token", "mode": "api"}, "type": "okta"},
                ["groups.okta_domain"],
            ),
            ({"issuer": ""}, ["issuer"]),
            ({"client_id_env": ""}, ["client_id"]),
            (
                {"groups": {"okta_domain": "acme.okta.com"}, "type": "entra_id"},
                ["groups.okta_domain"],
            ),
            ({"groups": {"sync_interval": 30}}, ["groups.sync_interval"]),
            ({"groups": {"api_token": "okta-ssws-token"}}, ["groups.api_token"]),
            (
                {"groups": {"tenant_id": "evil.example/../x"}, "type": "entra_id"},
                ["groups.tenant_id"],
            ),
        ],
        ids=[
            "generic-api-mode",
            "okta-api-without-domain",
            "no-issuer",
            "no-client-id",
            "another-types-field",
            "sync-under-a-minute",
            "directory-token-on-generic",
            "tenant-id-not-a-guid-or-domain",
        ],
    )
    def test_create_refuses_and_writes_nothing(
        self, admin_headers, api_settings, client, overrides, fields
    ):
        written = [
            Path(api_settings.secrets.path),
            Path(api_settings.auth.auth_dir) / "oidc-providers",
        ]
        before = [sorted(path.rglob("*")) for path in written]

        resp = _create_provider(
            client, admin_headers, name="refused", client_secret="rp-client-secret", **overrides
        )

        assert resp.status_code == 422, resp.text
        assert _field_errors(resp) == fields
        assert [sorted(path.rglob("*")) for path in written] == before

    def test_update_refuses_and_writes_nothing(self, client, app, admin_headers):
        _create_provider(
            client, admin_headers, name="okta-claim", type="okta", client_secret="first"
        )
        resp = client.put(
            "/api/v1/auth/oidc-providers/okta-claim",
            json={
                "client_secret": "second",
                "groups": {"api_token": "okta-ssws-token", "mode": "api"},
            },
            headers=admin_headers,
        )
        assert resp.status_code == 422, resp.text
        assert _field_errors(resp) == ["groups.okta_domain"]
        assert app.state.dfe_secrets.get("oidc/okta-claim/client_secret") == "first"
        assert not app.state.dfe_secrets.exists("oidc/okta-claim/groups_api_token")
        got = client.get("/api/v1/auth/oidc-providers/okta-claim", headers=admin_headers)
        assert got.json()["groups"]["mode"] == "token_claim"

    def test_a_toggle_skips_the_rules(self, client, app, admin_headers):
        # A provider written out of band that breaks the rules can still be switched off.
        from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider

        app.state.oidc_provider_registry.create(
            "legacy",
            OIDCProvider(
                type="generic",
                issuer="https://sso.example.com",
                groups=GroupResolutionConfig(mode="api"),
            ),
        )
        resp = client.put(
            "/api/v1/auth/oidc-providers/legacy", json={"enabled": False}, headers=admin_headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["enabled"] is False

    def test_a_groups_edit_keeps_the_directory_backend(self, client, app, admin_headers):
        # The directory backend is set in YAML only, so the API must not reset it.
        from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider

        app.state.oidc_provider_registry.create(
            "mock-okta",
            OIDCProvider(
                type="okta",
                issuer="https://acme.okta.com",
                client_id="c",
                groups=GroupResolutionConfig(
                    api_token_env="OKTA_API_TOKEN",
                    directory_backend="mock",
                    mock_directory_env="DFE_TEST_SYNC_DIRECTORY",
                    mode="api",
                    okta_domain="acme.okta.com",
                ),
            ),
        )
        resp = client.put(
            "/api/v1/auth/oidc-providers/mock-okta",
            json={
                "groups": {
                    "api_token_env": "OKTA_API_TOKEN",
                    "mode": "api",
                    "okta_domain": "acme.okta.com",
                    "sync_interval": 900,
                }
            },
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        stored = app.state.oidc_provider_registry.get("mock-okta").groups
        assert (stored.directory_backend, stored.mock_directory_env) == (
            "mock",
            "DFE_TEST_SYNC_DIRECTORY",
        )

    def test_google_always_enriches_on_login(self, client, admin_headers):
        resp = _create_provider(
            client,
            admin_headers,
            name="gws",
            type="google",
            issuer="https://accounts.google.com",
            groups={"mode": "api", "service_account_json_env": "GOOGLE_SA_JSON"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["groups"]["enrich_on_login"] is True

    def test_google_needs_no_service_account_and_names_no_admin(self, client, app, admin_headers):
        resp = _create_provider(client, admin_headers, name="gws-bare", **_GOOGLE)
        assert resp.status_code == 201, resp.text
        assert "admin_email" not in resp.json()["groups"]
        stored = app.state.oidc_provider_registry.get("gws-bare").groups
        assert (stored.service_account_json_path, stored.service_account_json_env) == ("", "")

    def test_an_update_is_not_refused_for_an_issuer_it_cannot_set(self, client, app, admin_headers):
        from dfe_engine.auth.oidc.models import OIDCProvider

        app.state.oidc_provider_registry.create(
            "no-issuer", OIDCProvider(type="generic", client_id="c")
        )
        resp = client.put(
            "/api/v1/auth/oidc-providers/no-issuer",
            json={"groups": {"mode": "manual"}},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
