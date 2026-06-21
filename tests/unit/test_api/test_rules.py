"""Tests for the rules router — RuleCreationService create + validate + CRUD."""


def _sample_create_payload(**overrides):
    payload = {
        "name": "Test Rule",
        "severity": "high",
        "user_sql": "SELECT * FROM default.events WHERE severity = 'high'",
        "source": "windows_audit",
    }
    payload.update(overrides)
    return payload


class TestRulesValidate:
    """POST /api/v1/rules/validate — validate SQL without creating."""

    def test_validate_valid_sql(self, client, admin_headers):
        resp = client.post(
            "/api/v1/rules/validate",
            json={"sql": "severity = 'high' AND user != 'admin'"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "valid" in data
        assert "errors" in data

    def test_validate_requires_auth(self, client):
        resp = client.post(
            "/api/v1/rules/validate",
            json={"sql": "severity = 'high'"},
        )
        assert resp.status_code == 401

    def test_validate_viewer_forbidden(self, client, viewer_headers):
        resp = client.post(
            "/api/v1/rules/validate",
            json={"sql": "event_type = 'login'"},
            headers=viewer_headers,
        )
        assert resp.status_code == 403

    def test_validate_missing_sql_field(self, client, admin_headers):
        resp = client.post(
            "/api/v1/rules/validate",
            json={},
            headers=admin_headers,
        )
        assert resp.status_code == 422
        assert resp.json()["code"] == "validation_error"


class TestRulesListAndDetail:
    """GET /api/v1/rules and GET /api/v1/rules/{rule_id}."""

    def test_list_empty(self, client, admin_headers):
        resp = client.get("/api/v1/rules", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_list_after_create_with_search(self, client, admin_headers):
        create = client.post(
            "/api/v1/rules",
            json=_sample_create_payload(name="Brute Force Rule"),
            headers=admin_headers,
        )
        assert create.status_code == 201
        rule_id = create.json()["rule"]["rule_id"]

        resp = client.get(
            "/api/v1/rules",
            params={"search": "brute"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["rule_id"] == rule_id

        miss = client.get(
            "/api/v1/rules",
            params={"search": "nonexistent"},
            headers=admin_headers,
        )
        assert miss.json()["total"] == 0

    def test_get_rule_detail(self, client, admin_headers):
        create = client.post(
            "/api/v1/rules",
            json=_sample_create_payload(),
            headers=admin_headers,
        )
        assert create.status_code == 201
        rule_id = create.json()["rule"]["rule_id"]

        resp = client.get(f"/api/v1/rules/{rule_id}", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["rule_id"] == rule_id
        assert resp.json()["name"] == "Test Rule"

    def test_get_rule_not_found(self, client, admin_headers):
        resp = client.get("/api/v1/rules/does-not-exist", headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_list_requires_read_permission(self, client, viewer_headers):
        resp = client.get("/api/v1/rules", headers=viewer_headers)
        assert resp.status_code == 200


class TestRulesUpdateAndDelete:
    """PUT and DELETE /api/v1/rules/{rule_id}."""

    def test_update_rule(self, client, admin_headers):
        create = client.post(
            "/api/v1/rules",
            json=_sample_create_payload(),
            headers=admin_headers,
        )
        assert create.status_code == 201
        rule_id = create.json()["rule"]["rule_id"]
        created_at = create.json()["rule"]["created_at"]

        resp = client.put(
            f"/api/v1/rules/{rule_id}",
            json=_sample_create_payload(name="Updated Rule Name"),
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["rule"]["name"] == "Updated Rule Name"
        assert resp.json()["rule"]["created_at"] == created_at

    def test_delete_rule(self, client, admin_headers):
        create = client.post(
            "/api/v1/rules",
            json=_sample_create_payload(),
            headers=admin_headers,
        )
        assert create.status_code == 201
        rule_id = create.json()["rule"]["rule_id"]

        delete = client.delete(f"/api/v1/rules/{rule_id}", headers=admin_headers)
        assert delete.status_code == 204

        get_resp = client.get(f"/api/v1/rules/{rule_id}", headers=admin_headers)
        assert get_resp.status_code == 404

    def test_delete_requires_delete_permission(self, client, viewer_headers, admin_headers):
        create = client.post(
            "/api/v1/rules",
            json=_sample_create_payload(),
            headers=admin_headers,
        )
        rule_id = create.json()["rule"]["rule_id"]

        resp = client.delete(f"/api/v1/rules/{rule_id}", headers=viewer_headers)
        assert resp.status_code == 403


class TestRulesCreate:
    """POST /api/v1/rules — create rule via RuleCreationService."""

    def test_create_basic_rule(self, client, admin_headers):
        resp = client.post(
            "/api/v1/rules",
            json=_sample_create_payload(),
            headers=admin_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "rule" in data
        assert data["rule"]["name"] == "Test Rule"
        assert data["rule"]["severity"] == "high"
        assert "rule_id" in data["rule"]

    def test_create_requires_write_permission(self, client, viewer_headers):
        resp = client.post(
            "/api/v1/rules",
            json={
                "name": "Test Rule",
                "severity": "low",
                "user_sql": "event_type = 'login'",
            },
            headers=viewer_headers,
        )
        assert resp.status_code == 403

    def test_create_missing_required_fields(self, client, admin_headers):
        resp = client.post(
            "/api/v1/rules",
            json={"severity": "high"},  # Missing name + user_sql
            headers=admin_headers,
        )
        assert resp.status_code == 422
        data = resp.json()
        assert data["code"] == "validation_error"

    def test_validate_returns_valid_boolean(self, client, admin_headers):
        resp = client.post(
            "/api/v1/rules/validate",
            json={"sql": "SELECT * FROM events WHERE user = 'admin'"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["valid"], bool)
        assert isinstance(data["errors"], list)
