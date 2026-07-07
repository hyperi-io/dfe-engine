#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_orgs.py
#  Purpose:      Tests for org CRUD REST endpoints
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for /api/v1/orgs endpoints.

Uses the shared conftest fixtures (client, admin_headers, viewer_headers).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# App wiring (lifespan)
# ---------------------------------------------------------------------------


class TestAppWiring:
    """Task E: the app lifespan builds OrgLifecycleManager WITH the secrets seam."""

    def test_org_lifecycle_has_secrets_store_and_hyperdx_flags(self, client):
        # The `client` fixture enters the TestClient context, running the lifespan
        # that constructs app.state.org_lifecycle.
        manager = client.app.state.org_lifecycle
        assert manager._secrets is not None  # secrets_store wired (per-org conn creds)
        assert manager._per_group is False  # DFE_HYPERDX_PER_GROUP GA default
        assert manager._ga_team_name == "dfe"


# ---------------------------------------------------------------------------
# POST /api/v1/orgs
# ---------------------------------------------------------------------------


class TestCreateOrg:
    def test_create_org_returns_201(self, client, admin_headers):
        resp = client.post(
            "/api/v1/orgs",
            json={"name": "acme", "org_ids": ["acme", "acme-sub"]},
            headers=admin_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "acme"
        assert data["org_ids"] == ["acme", "acme-sub"]
        assert data["enabled"] is True

    def test_create_org_with_display_name(self, client, admin_headers):
        resp = client.post(
            "/api/v1/orgs",
            json={"name": "acme", "display_name": "Acme Corp"},
            headers=admin_headers,
        )
        assert resp.status_code == 201
        assert resp.json()["display_name"] == "Acme Corp"

    def test_create_org_duplicate_returns_409(self, client, admin_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        resp = client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "conflict"

    def test_create_org_viewer_forbidden(self, client, viewer_headers):
        resp = client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=viewer_headers,
        )
        assert resp.status_code == 403

    def test_create_org_no_auth_returns_401(self, client):
        resp = client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
        )
        assert resp.status_code == 401

    def test_create_org_sets_timestamps(self, client, admin_headers):
        resp = client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        data = resp.json()
        assert data["created_at"] != ""
        assert data["updated_at"] != ""


# ---------------------------------------------------------------------------
# GET /api/v1/orgs
# ---------------------------------------------------------------------------


class TestListOrgs:
    def test_list_empty_returns_empty(self, client, admin_headers):
        resp = client.get("/api/v1/orgs", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["items"] == []

    def test_list_returns_created_orgs(self, client, admin_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        client.post(
            "/api/v1/orgs",
            json={"name": "beta"},
            headers=admin_headers,
        )
        resp = client.get("/api/v1/orgs", headers=admin_headers)
        assert resp.status_code == 200
        names = {o["name"] for o in resp.json()["items"]}
        assert names == {"acme", "beta"}

    def test_list_viewer_can_read(self, client, admin_headers, viewer_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        resp = client.get("/api/v1/orgs", headers=viewer_headers)
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_list_sort_by_bool_field(self, client, admin_headers):
        # sort_by a non-string (bool) field must not 500 on the mixed-type key
        # (regression: apply_sort used to raise TypeError on '<' for bool/str).
        client.post("/api/v1/orgs", json={"name": "acme"}, headers=admin_headers)
        client.post("/api/v1/orgs", json={"name": "beta"}, headers=admin_headers)
        resp = client.get("/api/v1/orgs?sort_by=enabled", headers=admin_headers)
        assert resp.status_code == 200
        assert {o["name"] for o in resp.json()["items"]} == {"acme", "beta"}

    def test_list_sort_by_list_field(self, client, admin_headers):
        # sort_by a list field (org_ids) is deterministic, not a 500.
        client.post(
            "/api/v1/orgs",
            json={"name": "acme", "org_ids": ["1", "2"]},
            headers=admin_headers,
        )
        client.post(
            "/api/v1/orgs",
            json={"name": "beta", "org_ids": ["3"]},
            headers=admin_headers,
        )
        resp = client.get("/api/v1/orgs?sort_by=org_ids", headers=admin_headers)
        assert resp.status_code == 200
        assert {o["name"] for o in resp.json()["items"]} == {"acme", "beta"}


# ---------------------------------------------------------------------------
# GET /api/v1/orgs/{name}
# ---------------------------------------------------------------------------


class TestGetOrg:
    def test_get_existing_org(self, client, admin_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme", "display_name": "Acme Corp"},
            headers=admin_headers,
        )
        resp = client.get("/api/v1/orgs/acme", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["name"] == "acme"
        assert resp.json()["display_name"] == "Acme Corp"

    def test_get_nonexistent_returns_404(self, client, admin_headers):
        resp = client.get("/api/v1/orgs/nobody", headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_get_viewer_can_read(self, client, admin_headers, viewer_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        resp = client.get("/api/v1/orgs/acme", headers=viewer_headers)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# PUT /api/v1/orgs/{name}
# ---------------------------------------------------------------------------


class TestUpdateOrg:
    def test_update_display_name(self, client, admin_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        resp = client.put(
            "/api/v1/orgs/acme",
            json={"display_name": "Acme Updated"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Acme Updated"

    def test_update_org_ids(self, client, admin_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme", "org_ids": ["acme"]},
            headers=admin_headers,
        )
        resp = client.put(
            "/api/v1/orgs/acme",
            json={"org_ids": ["acme", "acme-new"]},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["org_ids"] == ["acme", "acme-new"]

    def test_update_disable_org(self, client, admin_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        resp = client.put(
            "/api/v1/orgs/acme",
            json={"enabled": False},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    def test_update_nonexistent_returns_404(self, client, admin_headers):
        resp = client.put(
            "/api/v1/orgs/nobody",
            json={"display_name": "?"},
            headers=admin_headers,
        )
        assert resp.status_code == 404

    def test_update_viewer_forbidden(self, client, admin_headers, viewer_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        resp = client.put(
            "/api/v1/orgs/acme",
            json={"display_name": "Hacked"},
            headers=viewer_headers,
        )
        assert resp.status_code == 403

    def test_update_org_emits_audit(self, client, admin_headers, monkeypatch):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        import dfe_engine.api.v1.orgs as orgs_mod

        calls = []
        monkeypatch.setattr(orgs_mod, "audit_resource_change", lambda *a: calls.append(a))
        resp = client.put(
            "/api/v1/orgs/acme",
            json={"display_name": "Acme Updated"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert calls == [("admin", "org", "acme", "updated")]

    def test_update_org_no_fields_does_not_emit_audit(self, client, admin_headers, monkeypatch):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        import dfe_engine.api.v1.orgs as orgs_mod

        calls = []
        monkeypatch.setattr(orgs_mod, "audit_resource_change", lambda *a: calls.append(a))
        resp = client.put(
            "/api/v1/orgs/acme",
            json={},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert calls == []


# ---------------------------------------------------------------------------
# DELETE /api/v1/orgs/{name}
# ---------------------------------------------------------------------------


class TestDeleteOrg:
    def test_delete_returns_204(self, client, admin_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        resp = client.delete("/api/v1/orgs/acme", headers=admin_headers)
        assert resp.status_code == 204

    def test_delete_removes_org(self, client, admin_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        client.delete("/api/v1/orgs/acme", headers=admin_headers)
        resp = client.get("/api/v1/orgs/acme", headers=admin_headers)
        assert resp.status_code == 404

    def test_delete_nonexistent_returns_404(self, client, admin_headers):
        resp = client.delete("/api/v1/orgs/nobody", headers=admin_headers)
        assert resp.status_code == 404

    def test_delete_viewer_forbidden(self, client, admin_headers, viewer_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        resp = client.delete("/api/v1/orgs/acme", headers=viewer_headers)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Domains (Task A) - request/response carry email domains, lowercased
# ---------------------------------------------------------------------------


class TestOrgDomains:
    def test_create_org_with_domains_lowercased(self, client, admin_headers):
        resp = client.post(
            "/api/v1/orgs",
            json={"name": "acme", "domains": ["Acme.com", "ACME.io"]},
            headers=admin_headers,
        )
        assert resp.status_code == 201
        assert resp.json()["domains"] == ["acme.com", "acme.io"]

    def test_create_org_domains_default_empty(self, client, admin_headers):
        resp = client.post("/api/v1/orgs", json={"name": "acme"}, headers=admin_headers)
        assert resp.status_code == 201
        assert resp.json()["domains"] == []

    def test_get_org_returns_domains(self, client, admin_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme", "domains": ["acme.com"]},
            headers=admin_headers,
        )
        resp = client.get("/api/v1/orgs/acme", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["domains"] == ["acme.com"]

    def test_update_org_adds_and_removes_domains(self, client, admin_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme", "domains": ["acme.com"]},
            headers=admin_headers,
        )
        resp = client.put(
            "/api/v1/orgs/acme",
            json={"domains": ["Acme.com", "acme.io"]},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["domains"] == ["acme.com", "acme.io"]

        resp = client.put(
            "/api/v1/orgs/acme",
            json={"domains": ["acme.io"]},
            headers=admin_headers,
        )
        assert resp.json()["domains"] == ["acme.io"]


# ---------------------------------------------------------------------------
# Org CRUD -> background CH RBAC reconcile (Task D)
# ---------------------------------------------------------------------------


class TestOrgCrudReconcile:
    """Org CRUD schedules an idempotent background reconcile when CH is configured
    AND DFE_ORG_PROVISIONING_ENABLED; never blocks or fails the CRUD."""

    def _patch_reconcile(self, monkeypatch, *, raises=False):
        """Patch the CH boundary (no live CH) + record reconcile_ch_rbac calls."""
        from types import SimpleNamespace

        import dfe_engine.governance.ch as ch_mod
        import dfe_engine.secrets as secrets_mod
        from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager

        calls: list[dict] = []

        stub = SimpleNamespace(get_clickhouse_client=lambda: SimpleNamespace(_client=object()))
        monkeypatch.setattr(ClickHouseManager, "get_instance", staticmethod(lambda cfg: stub))
        monkeypatch.setattr(secrets_mod, "build_secrets", lambda cfg: None)

        def _recorder(*args, **kwargs):
            calls.append(kwargs)
            if raises:
                raise RuntimeError("ClickHouse unavailable")
            return SimpleNamespace(statements=[], dropped=[], minted=[], errors=[])

        monkeypatch.setattr(ch_mod, "reconcile_ch_rbac", _recorder)
        return calls

    def test_create_org_schedules_reconcile_when_enabled(self, client, admin_headers, monkeypatch):
        monkeypatch.setenv("DFE_ORG_PROVISIONING_ENABLED", "true")
        calls = self._patch_reconcile(monkeypatch)

        resp = client.post("/api/v1/orgs", json={"name": "acme"}, headers=admin_headers)
        assert resp.status_code == 201
        assert len(calls) == 1
        assert "orgs" in calls[0]  # reconcile fed the current org list

    def test_update_org_schedules_reconcile_when_enabled(self, client, admin_headers, monkeypatch):
        client.post("/api/v1/orgs", json={"name": "acme"}, headers=admin_headers)
        monkeypatch.setenv("DFE_ORG_PROVISIONING_ENABLED", "true")
        calls = self._patch_reconcile(monkeypatch)

        resp = client.put("/api/v1/orgs/acme", json={"display_name": "Acme"}, headers=admin_headers)
        assert resp.status_code == 200
        assert len(calls) == 1

    def test_delete_org_schedules_reconcile_when_enabled(self, client, admin_headers, monkeypatch):
        client.post("/api/v1/orgs", json={"name": "acme"}, headers=admin_headers)
        monkeypatch.setenv("DFE_ORG_PROVISIONING_ENABLED", "true")
        calls = self._patch_reconcile(monkeypatch)

        resp = client.delete("/api/v1/orgs/acme", headers=admin_headers)
        assert resp.status_code == 204
        assert len(calls) == 1

    def test_reconcile_error_does_not_fail_crud(self, client, admin_headers, monkeypatch):
        monkeypatch.setenv("DFE_ORG_PROVISIONING_ENABLED", "true")
        calls = self._patch_reconcile(monkeypatch, raises=True)

        resp = client.post("/api/v1/orgs", json={"name": "acme"}, headers=admin_headers)
        # Reconcile blew up in the background but the CRUD still succeeded.
        assert resp.status_code == 201
        assert len(calls) == 1
        assert client.get("/api/v1/orgs/acme", headers=admin_headers).status_code == 200

    def test_provisioning_off_does_not_reconcile(self, client, admin_headers, monkeypatch):
        monkeypatch.delenv("DFE_ORG_PROVISIONING_ENABLED", raising=False)
        calls = self._patch_reconcile(monkeypatch)

        resp = client.post("/api/v1/orgs", json={"name": "acme"}, headers=admin_headers)
        assert resp.status_code == 201
        assert calls == []  # gate closed -> no reconcile scheduled

    def test_clickhouse_not_configured_does_not_reconcile(self, client, admin_headers, monkeypatch):
        monkeypatch.setenv("DFE_ORG_PROVISIONING_ENABLED", "true")
        monkeypatch.setattr(client.app.state.settings.clickhouse, "host", "")
        calls = self._patch_reconcile(monkeypatch)

        resp = client.post("/api/v1/orgs", json={"name": "acme"}, headers=admin_headers)
        assert resp.status_code == 201
        assert calls == []  # CH not configured -> no reconcile scheduled
