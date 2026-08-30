"""Tests for the rules router — RuleCreationService create + validate + CRUD."""

import pytest


def _sample_create_payload(**overrides):
    payload = {
        "name": "test_rule",
        "display_name": "Test Rule",
        "severity": "high",
        "user_sql": "SELECT * FROM default.events WHERE severity = 'high'",
        "source": "windows-audit",
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
    """GET /api/v1/rules and GET /api/v1/rules/{name}."""

    def test_list_empty(self, client, admin_headers):
        resp = client.get("/api/v1/rules", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_list_after_create_with_search(self, client, admin_headers):
        create = client.post(
            "/api/v1/rules",
            json=_sample_create_payload(name="brute_force", display_name="Brute Force Rule"),
            headers=admin_headers,
        )
        assert create.status_code == 201
        rule_name = create.json()["rule"]["name"]

        resp = client.get(
            "/api/v1/rules",
            params={"search": "brute"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["name"] == rule_name

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
        rule_name = create.json()["rule"]["name"]

        resp = client.get(f"/api/v1/rules/{rule_name}", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["name"] == rule_name
        assert resp.json()["display_name"] == "Test Rule"
        assert resp.json()["sql_errors"] == []

    def test_get_rule_detail_includes_sql_errors(self, client, admin_headers):
        create = client.post(
            "/api/v1/rules",
            json=_sample_create_payload(
                name="bad_sql_rule", user_sql="INSERT INTO logs VALUES (1)"
            ),
            headers=admin_headers,
        )
        assert create.status_code == 201
        rule_name = create.json()["rule"]["name"]

        resp = client.get(f"/api/v1/rules/{rule_name}", headers=admin_headers)
        assert resp.status_code == 200
        assert len(resp.json()["sql_errors"]) > 0

    def test_get_rule_not_found(self, client, admin_headers):
        resp = client.get("/api/v1/rules/does-not-exist", headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_list_requires_read_permission(self, client, viewer_headers):
        resp = client.get("/api/v1/rules", headers=viewer_headers)
        assert resp.status_code == 200


class TestRulesUpdateAndDelete:
    """PUT and DELETE /api/v1/rules/{name}."""

    def test_update_rule(self, client, admin_headers):
        create = client.post(
            "/api/v1/rules",
            json=_sample_create_payload(),
            headers=admin_headers,
        )
        assert create.status_code == 201
        rule_name = create.json()["rule"]["name"]
        created_at = create.json()["rule"]["created_at"]

        resp = client.put(
            f"/api/v1/rules/{rule_name}",
            json=_sample_create_payload(display_name="Updated Rule Name"),
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["rule"]["display_name"] == "Updated Rule Name"
        assert resp.json()["rule"]["created_at"] == created_at

    def test_update_rule_ignores_source_type(self, client, admin_headers):
        create = client.post(
            "/api/v1/rules",
            json=_sample_create_payload(name="src_type_rule"),
            headers=admin_headers,
        )
        rule_name = create.json()["rule"]["name"]

        resp = client.put(
            f"/api/v1/rules/{rule_name}",
            json={
                "severity": "high",
                "user_sql": "SELECT * FROM default.events WHERE severity = 'high'",
                "source": "windows-audit",
                "source_type": "",
            },
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["rule"]["source"] == "windows-audit"

    def test_delete_rule(self, client, admin_headers):
        create = client.post(
            "/api/v1/rules",
            json=_sample_create_payload(name="delete_me"),
            headers=admin_headers,
        )
        assert create.status_code == 201
        rule_name = create.json()["rule"]["name"]

        delete = client.delete(f"/api/v1/rules/{rule_name}", headers=admin_headers)
        assert delete.status_code == 204

        get_resp = client.get(f"/api/v1/rules/{rule_name}", headers=admin_headers)
        assert get_resp.status_code == 404

    def test_delete_blocked_when_referenced_by_hunt(self, client, admin_headers):
        rule_name = "hunt_linked_rule"
        create = client.post(
            "/api/v1/rules",
            json=_sample_create_payload(name=rule_name),
            headers=admin_headers,
        )
        assert create.status_code == 201

        hunt_payload = {
            "name": "rule_guard_hunt",
            "cron": "* * * * *",
            "global_target_table_name": "logs_alerts",
            "global_source_table_name": "logs_nxlog",
            "customers": ["org_a"],
            "rules": [rule_name],
        }
        assert (
            client.post("/api/v1/hunts", json=hunt_payload, headers=admin_headers).status_code
            == 201
        )

        delete = client.delete(f"/api/v1/rules/{rule_name}", headers=admin_headers)
        assert delete.status_code == 409
        body = delete.json()
        assert body["code"] == "conflict"
        assert rule_name in body["message"]
        assert "rule_guard_hunt" in body["message"]

        assert client.get(f"/api/v1/rules/{rule_name}", headers=admin_headers).status_code == 200
        client.delete("/api/v1/hunts/rule_guard_hunt", headers=admin_headers)
        assert client.delete(f"/api/v1/rules/{rule_name}", headers=admin_headers).status_code == 204

    def test_delete_requires_delete_permission(self, client, viewer_headers, admin_headers):
        create = client.post(
            "/api/v1/rules",
            json=_sample_create_payload(name="viewer_del_test"),
            headers=admin_headers,
        )
        rule_name = create.json()["rule"]["name"]

        resp = client.delete(f"/api/v1/rules/{rule_name}", headers=viewer_headers)
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
        assert data["rule"]["display_name"] == "Test Rule"
        assert data["rule"]["name"] == "test_rule"
        assert data["rule"]["severity"] == "high"

    def test_create_default_display_name(self, client, admin_headers):
        resp = client.post(
            "/api/v1/rules",
            json={
                "name": "my_rule",
                "severity": "high",
                "user_sql": "SELECT * FROM default.events WHERE severity = 'high'",
                "source": "windows-audit",
            },
            headers=admin_headers,
        )
        assert resp.status_code == 201
        assert resp.json()["rule"]["display_name"] == "My rule"

    def test_create_duplicate_name_returns_409(self, client, admin_headers):
        payload = _sample_create_payload(name="dup_rule")
        assert client.post("/api/v1/rules", json=payload, headers=admin_headers).status_code == 201
        dup = client.post("/api/v1/rules", json=payload, headers=admin_headers)
        assert dup.status_code == 409

    def test_create_duplicate_name_case_insensitive_returns_409(self, client, admin_headers):
        first = _sample_create_payload(name="MyRule")
        assert client.post("/api/v1/rules", json=first, headers=admin_headers).status_code == 201
        second = _sample_create_payload(name="myrule")
        dup = client.post("/api/v1/rules", json=second, headers=admin_headers)
        assert dup.status_code == 409

    def test_create_invalid_name_returns_422(self, client, admin_headers):
        resp = client.post(
            "/api/v1/rules",
            json=_sample_create_payload(name="bad/name"),
            headers=admin_headers,
        )
        assert resp.status_code == 422

    def test_create_requires_write_permission(self, client, viewer_headers):
        resp = client.post(
            "/api/v1/rules",
            json={
                "name": "viewer_rule",
                "severity": "low",
                "user_sql": "event_type = 'login'",
            },
            headers=viewer_headers,
        )
        assert resp.status_code == 403

    def test_create_missing_required_fields(self, client, admin_headers):
        resp = client.post(
            "/api/v1/rules",
            json={"severity": "high"},
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


class _FakeHyperdx:
    """Stands in for HyperDXClient, recording which saved search was asked for."""

    def __init__(self) -> None:
        self.rendered: dict | None = None
        self.asked_for: str | None = None

    async def saved_search_sql(self, saved_search_id: str) -> dict | None:
        self.asked_for = saved_search_id
        return self.rendered


@pytest.fixture
def fake_hyperdx(client):
    """Install a stand-in HyperDX client on the app, and put back what was there."""
    fake = _FakeHyperdx()
    previous = getattr(client.app.state, "hyperdx_client", None)
    client.app.state.hyperdx_client = fake
    yield fake
    client.app.state.hyperdx_client = previous


class TestRulesFromHyperdx:
    """POST /api/v1/rules/from-hyperdx — create a rule from a HyperDX view."""

    _RAW_SQL = "SELECT * FROM default.events WHERE severity = 'high'"

    def test_from_hyperdx_creates_rule_and_returns_id(self, client, admin_headers):
        resp = client.post(
            "/api/v1/rules/from-hyperdx",
            json={"raw_sql": self._RAW_SQL, "saved_search_name": "High Severity Logins"},
            headers=admin_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"] == "high-severity-logins"
        assert data["display_name"] == "High Severity Logins"

    def test_from_hyperdx_default_id_when_no_name(self, client, admin_headers):
        resp = client.post(
            "/api/v1/rules/from-hyperdx",
            json={"raw_sql": self._RAW_SQL},
            headers=admin_headers,
        )
        assert resp.status_code == 201
        assert resp.json()["id"] == "hyperdx-rule"

    def test_from_hyperdx_dedupes_id(self, client, admin_headers):
        body = {"raw_sql": self._RAW_SQL, "saved_search_name": "Repeat Search"}
        first = client.post("/api/v1/rules/from-hyperdx", json=body, headers=admin_headers)
        second = client.post("/api/v1/rules/from-hyperdx", json=body, headers=admin_headers)
        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["id"] == "repeat-search"
        assert second.json()["id"] == "repeat-search-2"

    def test_from_hyperdx_requires_write_permission(self, client, viewer_headers):
        resp = client.post(
            "/api/v1/rules/from-hyperdx",
            json={"raw_sql": self._RAW_SQL, "saved_search_name": "Viewer Attempt"},
            headers=viewer_headers,
        )
        assert resp.status_code == 403

    def test_from_hyperdx_requires_auth(self, client):
        resp = client.post(
            "/api/v1/rules/from-hyperdx",
            json={"raw_sql": self._RAW_SQL},
        )
        assert resp.status_code == 401

    def test_from_hyperdx_requires_raw_sql(self, client, admin_headers):
        resp = client.post(
            "/api/v1/rules/from-hyperdx",
            json={"saved_search_name": "No SQL"},
            headers=admin_headers,
        )
        assert resp.status_code == 422

    def test_from_hyperdx_rejects_both_sql_sources(self, client, admin_headers):
        resp = client.post(
            "/api/v1/rules/from-hyperdx",
            json={"raw_sql": self._RAW_SQL, "saved_search_id": "abc123"},
            headers=admin_headers,
        )
        assert resp.status_code == 422

    def test_raw_sql_path_reports_its_source(self, client, admin_headers):
        resp = client.post(
            "/api/v1/rules/from-hyperdx",
            json={"raw_sql": self._RAW_SQL, "saved_search_name": "Trusted String"},
            headers=admin_headers,
        )
        assert resp.status_code == 201
        assert resp.json()["resolved_from"] == "raw_sql"


class TestRulesFromHyperdxSavedSearch:
    """POST /api/v1/rules/from-hyperdx with a saved-search id.

    The point of the id path is that the rule is built from what the VIEW runs,
    so these assert the engine asked HyperDX and used the answer.
    """

    _VIEW_SQL = "SELECT * FROM default.events WHERE action = 'delete'"

    def test_builds_the_rule_from_the_views_sql(self, client, admin_headers, fake_hyperdx):
        fake_hyperdx.rendered = {"rawSql": self._VIEW_SQL, "savedSearchName": "Mass Delete"}
        resp = client.post(
            "/api/v1/rules/from-hyperdx",
            json={"saved_search_id": "srch_1"},
            headers=admin_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert fake_hyperdx.asked_for == "srch_1"
        assert data["resolved_from"] == "saved_search"
        assert data["id"] == "mass-delete"
        assert data["display_name"] == "Mass Delete"

    def test_caller_name_wins_over_the_rendered_one(self, client, admin_headers, fake_hyperdx):
        fake_hyperdx.rendered = {"rawSql": self._VIEW_SQL, "savedSearchName": "Rendered Name"}
        resp = client.post(
            "/api/v1/rules/from-hyperdx",
            json={"saved_search_id": "srch_2", "saved_search_name": "Caller Name"},
            headers=admin_headers,
        )
        assert resp.status_code == 201
        assert resp.json()["display_name"] == "Caller Name"

    def test_unrenderable_view_is_502_not_a_rule(self, client, admin_headers, fake_hyperdx):
        # A rule invented from no SQL would be worse than no rule.
        fake_hyperdx.rendered = None
        resp = client.post(
            "/api/v1/rules/from-hyperdx",
            json={"saved_search_id": "missing"},
            headers=admin_headers,
        )
        assert resp.status_code == 502
        assert resp.json()["code"] == "hyperdx_render_failed"

    def test_blank_sql_is_treated_as_no_answer(self, client, admin_headers, fake_hyperdx):
        fake_hyperdx.rendered = {"rawSql": "", "savedSearchName": "Empty"}
        resp = client.post(
            "/api/v1/rules/from-hyperdx",
            json={"saved_search_id": "empty"},
            headers=admin_headers,
        )
        assert resp.status_code == 502

    def test_without_hyperdx_configured_is_503(self, client, admin_headers):
        client.app.state.hyperdx_client = None
        resp = client.post(
            "/api/v1/rules/from-hyperdx",
            json={"saved_search_id": "srch_3"},
            headers=admin_headers,
        )
        assert resp.status_code == 503
        assert resp.json()["code"] == "hyperdx_unconfigured"

    def test_still_requires_write_permission(self, client, viewer_headers, fake_hyperdx):
        fake_hyperdx.rendered = {"rawSql": self._VIEW_SQL, "savedSearchName": "Viewer Attempt"}
        resp = client.post(
            "/api/v1/rules/from-hyperdx",
            json={"saved_search_id": "srch_4"},
            headers=viewer_headers,
        )
        assert resp.status_code == 403
        assert fake_hyperdx.asked_for is None
