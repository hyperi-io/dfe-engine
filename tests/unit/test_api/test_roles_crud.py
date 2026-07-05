"""Tests for /api/v1/auth/roles endpoints."""


class TestRolesScopes:
    def test_list_scopes_requires_admin(self, client, viewer_headers):
        resp = client.get("/api/v1/auth/roles/scopes", headers=viewer_headers)
        assert resp.status_code == 403

    def test_list_scopes(self, client, admin_headers):
        resp = client.get("/api/v1/auth/roles/scopes", headers=admin_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert "scopes" in body
        assert "total" in body
        assert "config:read" in body["scopes"]
        assert body["wildcard"] is True
        assert body["argo_namespace_prefix"] == "argo:"

    def test_list_scopes_search_and_prefix(self, client, admin_headers):
        resp = client.get(
            "/api/v1/auth/roles/scopes",
            params={"search": "config", "prefix": "config:", "per_page": 5, "page": 1},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["per_page"] == 5
        assert all(s.startswith("config:") for s in body["scopes"])
        assert all("config" in s.lower() for s in body["scopes"])


class TestRolesCrud:
    def test_list_roles_includes_builtin(self, client, admin_headers):
        resp = client.get("/api/v1/auth/roles", headers=admin_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert body["total"] >= 1
        by_name = {r["name"]: r for r in body["items"]}
        assert "admin" in by_name
        assert by_name["admin"]["resource_type"] == "core"

    def test_list_roles_filter_resource_type_core(self, client, admin_headers):
        resp = client.get(
            "/api/v1/auth/roles",
            params={"resource_type": "core"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        roles = resp.json()["items"]
        assert roles
        assert all(r["resource_type"] == "core" for r in roles)
        assert any(r["name"] == "admin" for r in roles)

    def test_list_roles_search_and_pagination(self, client, admin_headers):
        resp = client.get(
            "/api/v1/auth/roles",
            params={"search": "infra", "page": 1, "per_page": 2},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["per_page"] == 2
        assert body["page"] == 1
        assert len(body["items"]) <= 2
        assert all(
            "infra" in r["name"].lower() or "infra" in r["description"].lower()
            for r in body["items"]
        )
        assert body["total"] >= len(body["items"])

    def test_list_roles_filter_resource_type_custom(self, client, admin_headers):
        client.post(
            "/api/v1/auth/roles",
            json={
                "name": "filter_custom_role",
                "description": "",
                "permissions": ["query:execute"],
            },
            headers=admin_headers,
        )
        resp = client.get(
            "/api/v1/auth/roles",
            params={"resource_type": "custom"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        roles = resp.json()["items"]
        assert all(r["resource_type"] == "custom" for r in roles)
        assert "admin" not in {r["name"] for r in roles}
        client.delete("/api/v1/auth/roles/filter_custom_role", headers=admin_headers)

    def test_create_rejects_resource_type_in_body(self, client, admin_headers):
        resp = client.post(
            "/api/v1/auth/roles",
            json={
                "name": "bad_meta",
                "description": "",
                "permissions": ["query:execute"],
                "resource_type": "core",
            },
            headers=admin_headers,
        )
        assert resp.status_code == 422

    def test_create_get_update_delete(self, client, admin_headers):
        create = client.post(
            "/api/v1/auth/roles",
            json={
                "name": "api_test_role",
                "description": "Temporary",
                "permissions": ["query:execute"],
                "scoped": False,
            },
            headers=admin_headers,
        )
        assert create.status_code == 201
        body = create.json()
        assert body["name"] == "api_test_role"
        assert body["resource_type"] == "custom"

        get_one = client.get("/api/v1/auth/roles/api_test_role", headers=admin_headers)
        assert get_one.status_code == 200

        update = client.put(
            "/api/v1/auth/roles/api_test_role",
            json={"description": "Updated"},
            headers=admin_headers,
        )
        assert update.status_code == 200
        assert update.json()["description"] == "Updated"

        delete = client.delete("/api/v1/auth/roles/api_test_role", headers=admin_headers)
        assert delete.status_code == 204

        missing = client.get("/api/v1/auth/roles/api_test_role", headers=admin_headers)
        assert missing.status_code == 404

    def test_delete_role_in_use_returns_409(self, client, admin_headers):
        resp = client.delete("/api/v1/auth/roles/admin", headers=admin_headers)
        assert resp.status_code == 409

    def test_delete_core_role_returns_409(self, client, admin_headers):
        resp = client.delete("/api/v1/auth/roles/infra_ro", headers=admin_headers)
        assert resp.status_code == 409
        data = resp.json()
        assert data["code"] == "conflict"
        assert data["message"] == "Core resources can't be mutated"
        assert len(resp.content) > 0

    def test_update_core_role_returns_409(self, client, admin_headers):
        resp = client.put(
            "/api/v1/auth/roles/admin",
            json={
                "description": "Full access to all resources and all orgs",
                "permissions": ["*"],
                "scoped": False,
            },
            headers=admin_headers,
        )
        assert resp.status_code == 409
        data = resp.json()
        assert data["code"] == "conflict"
        assert data["message"] == "Core resources can't be mutated"
        assert len(resp.content) > 0
