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
        assert "config:read" in body["scopes"]
        assert body["wildcard"] is True
        assert body["argo_namespace_prefix"] == "argo:"


class TestRolesCrud:
    def test_list_roles_includes_builtin(self, client, admin_headers):
        resp = client.get("/api/v1/auth/roles", headers=admin_headers)
        assert resp.status_code == 200
        by_name = {r["name"]: r for r in resp.json()}
        assert "admin" in by_name
        assert by_name["admin"]["resource_type"] == "core"

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
        resp = client.delete("/api/v1/auth/roles/infra_viewer", headers=admin_headers)
        assert resp.status_code == 409
        assert "core" in resp.json()["message"].lower()
