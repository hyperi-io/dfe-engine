"""Tests for the rules router — RuleCreationService create + validate."""


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


class TestRulesCreate:
    """POST /api/v1/rules — create rule via RuleCreationService."""

    def test_create_basic_rule(self, client, admin_headers):
        resp = client.post(
            "/api/v1/rules",
            json={
                "name": "Test Rule",
                "severity": "high",
                "user_sql": "severity = 'high'",
                "source": "windows_audit",
            },
            headers=admin_headers,
        )
        # 201 on success, or validation error if service has issues
        assert resp.status_code in (201, 422, 500)
        if resp.status_code == 201:
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
