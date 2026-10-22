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

import pytest

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

    def test_create_org_tenant_id_already_declared_returns_409(self, client, admin_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme", "org_ids": ["t-acme-1"]},
            headers=admin_headers,
        )
        resp = client.post(
            "/api/v1/orgs",
            json={"name": "beta", "org_ids": ["t-acme-1"]},
            headers=admin_headers,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "conflict"
        assert "acme" in resp.json()["message"]

    def test_create_org_tenant_id_equal_to_another_orgs_name_returns_409(
        self, client, admin_headers
    ):
        client.post("/api/v1/orgs", json={"name": "acme"}, headers=admin_headers)
        resp = client.post(
            "/api/v1/orgs",
            json={"name": "beta", "org_ids": ["acme"]},
            headers=admin_headers,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "conflict"

    def test_create_org_name_equal_to_another_orgs_tenant_id_returns_409(
        self, client, admin_headers
    ):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme", "org_ids": ["t-acme-1"]},
            headers=admin_headers,
        )
        resp = client.post(
            "/api/v1/orgs",
            json={"name": "t-acme-1"},
            headers=admin_headers,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "conflict"

    def test_create_org_may_keep_its_own_name_among_its_tenant_ids(self, client, admin_headers):
        resp = client.post(
            "/api/v1/orgs",
            json={"name": "acme", "org_ids": ["acme", "acme-sub"]},
            headers=admin_headers,
        )
        assert resp.status_code == 201
        assert resp.json()["org_ids"] == ["acme", "acme-sub"]

    def test_create_org_path_traversal_name_rejected(self, client, admin_headers):
        resp = client.post(
            "/api/v1/orgs",
            headers=admin_headers,
            json={"name": "../../escape"},
        )
        assert resp.status_code == 422
        listed = client.get("/api/v1/orgs", headers=admin_headers).json()
        assert [org["name"] for org in listed["items"]] == ["default"]

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
    def test_a_fresh_deployment_lists_the_default_org_alone(self, client, admin_headers):
        resp = client.get("/api/v1/orgs", headers=admin_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert [org["name"] for org in body["items"]] == ["default"]
        assert body["total"] == 1

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
        assert names == {"acme", "beta", "default"}

    def test_list_viewer_can_read(self, client, admin_headers, viewer_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme"},
            headers=admin_headers,
        )
        resp = client.get("/api/v1/orgs", headers=viewer_headers)
        assert resp.status_code == 200
        assert [o["name"] for o in resp.json()["items"]] == ["acme", "default"]


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

    def test_update_tenant_id_already_declared_returns_409(self, client, admin_headers):
        client.post(
            "/api/v1/orgs",
            json={"name": "acme", "org_ids": ["t-acme-1"]},
            headers=admin_headers,
        )
        client.post(
            "/api/v1/orgs",
            json={"name": "beta", "org_ids": ["t-beta-1"]},
            headers=admin_headers,
        )
        resp = client.put(
            "/api/v1/orgs/beta",
            json={"org_ids": ["t-acme-1"]},
            headers=admin_headers,
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "conflict"
        assert "acme" in resp.json()["message"]
        assert client.get("/api/v1/orgs/beta", headers=admin_headers).json()["org_ids"] == [
            "t-beta-1"
        ]

    def test_update_may_keep_its_own_name_among_its_tenant_ids(self, client, admin_headers):
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


def _first_write_lands_before(monkeypatch, app, method: str, write) -> None:
    """Make *write* land the moment the route calls the registry's *method*, as another replica's would."""
    registry = app.state.org_registry
    real = getattr(registry, method)

    def racing(*args, **kwargs):
        monkeypatch.setattr(registry, method, real)
        write()
        return real(*args, **kwargs)

    monkeypatch.setattr(registry, method, racing)


class TestAWriteThatLandsAfterTheChecks:
    """A collision another write makes after a route's checks is still a 409, never a 500."""

    def test_a_create_whose_tenant_id_was_just_taken_is_409(
        self, client, app, admin_headers, monkeypatch
    ):
        registry = app.state.org_registry
        _first_write_lands_before(
            monkeypatch, app, "create", lambda: registry.create("alpha", org_ids=["shared"])
        )

        resp = client.post(
            "/api/v1/orgs", json={"name": "beta", "org_ids": ["shared"]}, headers=admin_headers
        )

        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "conflict"
        assert registry.get("beta") is None

    def test_a_create_whose_name_was_just_taken_is_409(
        self, client, app, admin_headers, monkeypatch
    ):
        registry = app.state.org_registry
        _first_write_lands_before(monkeypatch, app, "create", lambda: registry.create("beta"))

        resp = client.post("/api/v1/orgs", json={"name": "beta"}, headers=admin_headers)

        assert resp.status_code == 409, resp.text

    def test_an_update_whose_tenant_id_was_just_taken_is_409(
        self, client, app, admin_headers, monkeypatch
    ):
        registry = app.state.org_registry
        registry.create("beta")
        _first_write_lands_before(
            monkeypatch, app, "update", lambda: registry.create("alpha", org_ids=["shared"])
        )

        resp = client.put("/api/v1/orgs/beta", json={"org_ids": ["shared"]}, headers=admin_headers)

        assert resp.status_code == 409, resp.text
        assert registry.get("beta").org_ids == []

    def test_an_update_of_an_org_just_deleted_is_404(self, client, app, admin_headers, monkeypatch):
        registry = app.state.org_registry
        registry.create("beta")
        _first_write_lands_before(monkeypatch, app, "update", lambda: registry.delete("beta"))

        resp = client.put("/api/v1/orgs/beta", json={"display_name": "B"}, headers=admin_headers)

        assert resp.status_code == 404, resp.text


# An empty id matches every untenanted row, and a comma splits the ClickHouse pin.
_FENCE_BREAKING_IDS = ["", " acme", "a,b", "acme\n"]


class TestTenantIdShape:
    @pytest.mark.parametrize("tenant", _FENCE_BREAKING_IDS)
    def test_create_refuses_a_fence_breaking_tenant_id(self, client, admin_headers, tenant):
        resp = client.post(
            "/api/v1/orgs", json={"name": "acme", "org_ids": [tenant]}, headers=admin_headers
        )

        assert resp.status_code == 422
        assert client.get("/api/v1/orgs/acme", headers=admin_headers).status_code == 404

    @pytest.mark.parametrize("tenant", _FENCE_BREAKING_IDS)
    def test_update_refuses_a_fence_breaking_tenant_id(self, client, admin_headers, tenant):
        client.post(
            "/api/v1/orgs", json={"name": "acme", "org_ids": ["t-acme-1"]}, headers=admin_headers
        )

        resp = client.put(
            "/api/v1/orgs/acme", json={"org_ids": ["t-acme-1", tenant]}, headers=admin_headers
        )

        assert resp.status_code == 422
        stored = client.get("/api/v1/orgs/acme", headers=admin_headers).json()
        assert stored["org_ids"] == ["t-acme-1"]

    def test_a_tenant_id_a_source_sends_verbatim_is_accepted(self, client, admin_headers):
        tenants = ["Acme Corp", "aws:123456789012", "3f2a9c1e-7b4d-4e8a-9f0e-2c6d1b8a5e3f"]

        resp = client.post(
            "/api/v1/orgs", json={"name": "acme", "org_ids": tenants}, headers=admin_headers
        )

        assert resp.status_code == 201
        assert resp.json()["org_ids"] == tenants


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
# GET /api/v1/orgs/available-ids
# ---------------------------------------------------------------------------


class _FakeClickHouse:
    def __init__(self, tables: list[str], ids: list[str]) -> None:
        self.tables = tables
        self.ids = ids

    def execute(self, sql: str, parameters=None, settings=None):
        if "system.columns" in sql:
            return [(t,) for t in self.tables]
        return [(i,) for i in self.ids]


def _use(app, ch) -> None:
    from dfe_engine.api.deps import get_clickhouse_client

    app.dependency_overrides[get_clickhouse_client] = lambda: ch


class TestAvailableOrgIds:
    def test_the_ids_the_data_carries_are_offered(self, client, app, admin_headers):
        _use(app, _FakeClickHouse(["filebeat"], ["acme", "globex"]))

        resp = client.get("/api/v1/orgs/available-ids", headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["org_ids"] == ["acme", "globex"]

    def test_a_deployment_with_no_ingested_data_offers_nothing(self, client, app, admin_headers):
        _use(app, _FakeClickHouse([], []))

        resp = client.get("/api/v1/orgs/available-ids", headers=admin_headers)

        assert resp.status_code == 200
        assert resp.json()["org_ids"] == []

    def test_the_literal_path_wins_over_the_org_name_route(self, client, app, admin_headers):
        # Declared above /{name}, so this is not read as an org called available-ids.
        _use(app, _FakeClickHouse(["filebeat"], ["acme"]))

        resp = client.get("/api/v1/orgs/available-ids", headers=admin_headers)

        assert resp.status_code == 200
        assert "org_ids" in resp.json()
        assert "display_name" not in resp.json()

    def test_a_viewer_is_refused(self, client, app, viewer_headers):
        _use(app, _FakeClickHouse(["filebeat"], ["acme"]))

        resp = client.get("/api/v1/orgs/available-ids", headers=viewer_headers)

        assert resp.status_code == 403

    def test_a_clickhouse_failure_is_reported_rather_than_read_as_no_ids(
        self, client, app, admin_headers
    ):
        class Broken:
            def execute(self, sql, parameters=None, settings=None):
                raise RuntimeError("clickhouse is down")

        _use(app, Broken())

        resp = client.get("/api/v1/orgs/available-ids", headers=admin_headers)

        assert resp.status_code == 503
        assert resp.json()["code"] == "discovery_failed"
