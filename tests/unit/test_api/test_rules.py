"""Tests for the rules router — RuleCreationService create + validate + CRUD."""

import pytest

from dfe_engine.api.deps import get_rule_registry
from dfe_engine.hunts.rule_model import Rule

# What HyperDX's search export emitted before it rendered the view's own table:
# the legacy template, which names no table and does not parse.
_HYPERDX_TEMPLATE_SQL = (
    "SELECT _timestamp,_json FROM {{org_id}}.{{source_table_name}} "
    "WHERE ({{timestamp_condition}}) ORDER BY _timestamp DESC"
)

# What the export renders for a search-bar condition and a side-panel filter on `main`.
_HYPERDX_RENDERED_SQL = (
    "SELECT _timestamp,_json FROM dfe.main WHERE (toString(`_tags`.marker) = 'run-1' "
    "AND (toString(_json.event_type) = 'login_failure')) AND "
    "((toString(_json.`user_name`) IN ('root'))) ORDER BY _timestamp DESC"
)


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
        # The API refuses such a rule, so it is written the way a hand-authored
        # rule file reaches the deploy repo.
        get_rule_registry().save(
            Rule(
                rule_id="bad_sql_rule",
                name="Bad SQL Rule",
                original_sql="INSERT INTO logs VALUES (1)",
            )
        )

        resp = client.get("/api/v1/rules/bad_sql_rule", headers=admin_headers)
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

    @pytest.mark.parametrize(
        "body",
        [
            {"saved_search_id": "srch_1"},
            {"saved_search_id": "srch_1", "raw_sql": _HYPERDX_RENDERED_SQL},
        ],
        ids=["id_alone", "id_beside_raw_sql"],
    )
    def test_a_saved_search_id_is_refused_and_writes_nothing(self, client, admin_headers, body):
        resp = client.post("/api/v1/rules/from-hyperdx", json=body, headers=admin_headers)

        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "validation_error"
        assert "saved_search_id" in resp.text
        assert client.get("/api/v1/rules", headers=admin_headers).json()["total"] == 0

    def test_from_hyperdx_refuses_empty_raw_sql(self, client, admin_headers):
        resp = client.post(
            "/api/v1/rules/from-hyperdx", json={"raw_sql": ""}, headers=admin_headers
        )
        assert resp.status_code == 422
        assert resp.json()["code"] == "validation_error"


def _assert_refused(resp) -> list[dict]:
    """The invalid_sql refusal, returning the SQL errors it carries."""
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "invalid_sql"
    sql_errors = body["context"]["sql_errors"]
    assert sql_errors
    assert all(error["message"] for error in sql_errors)
    assert sql_errors[0]["message"] in body["message"]
    return sql_errors


class TestRulesRefuseUncompilable:
    """A rule the hunt runner cannot compile is refused with 422 and never written."""

    def test_hyperdx_template_is_refused_and_not_saved(self, client, admin_headers):
        resp = client.post(
            "/api/v1/rules/from-hyperdx",
            json={"raw_sql": _HYPERDX_TEMPLATE_SQL},
            headers=admin_headers,
        )

        _assert_refused(resp)
        assert client.get("/api/v1/rules", headers=admin_headers).json()["total"] == 0
        assert client.get("/api/v1/rules/hyperdx-rule", headers=admin_headers).status_code == 404

    def test_rendered_hyperdx_view_keeps_its_table_and_filters(self, client, admin_headers):
        resp = client.post(
            "/api/v1/rules/from-hyperdx",
            json={"raw_sql": _HYPERDX_RENDERED_SQL, "saved_search_name": "Root rulecycle"},
            headers=admin_headers,
        )

        assert resp.status_code == 201, resp.text
        assert resp.json()["sql_errors"] == []
        rule = client.get("/api/v1/rules/root-rulecycle", headers=admin_headers).json()
        assert (rule["source_db"], rule["source_table"]) == ("dfe", "main")
        assert "toString(_json.event_type) = 'login_failure'" in rule["where_clause"]
        assert "toString(_json.`user_name`) IN ('root')" in rule["where_clause"]
        assert rule["sql_errors"] == []

    def test_search_with_no_filter_is_refused(self, client, admin_headers):
        # The template's time condition stripped, what is left matches every row.
        resp = client.post(
            "/api/v1/rules/from-hyperdx",
            json={"raw_sql": "SELECT _timestamp,_json FROM dfe.main ORDER BY _timestamp DESC"},
            headers=admin_headers,
        )

        sql_errors = _assert_refused(resp)
        assert [e["message"] for e in sql_errors] == [
            "Rule has empty detection logic (WHERE clause)."
        ]
        assert client.get("/api/v1/rules", headers=admin_headers).json()["total"] == 0

    def test_create_refuses_sql_that_does_not_parse(self, client, admin_headers):
        resp = client.post(
            "/api/v1/rules",
            json=_sample_create_payload(
                name="unparsed", user_sql="SELECT count() FROM default.events WHERE x = "
            ),
            headers=admin_headers,
        )

        _assert_refused(resp)
        assert client.get("/api/v1/rules/unparsed", headers=admin_headers).status_code == 404

    def test_create_refuses_a_from_with_no_database(self, client, admin_headers):
        resp = client.post(
            "/api/v1/rules",
            json=_sample_create_payload(
                name="unqualified", user_sql="SELECT * FROM events WHERE severity = 'high'"
            ),
            headers=admin_headers,
        )

        sql_errors = _assert_refused(resp)
        assert any("<db>.<table>" in error["message"] for error in sql_errors)
        assert client.get("/api/v1/rules/unqualified", headers=admin_headers).status_code == 404

    def test_create_refuses_dml(self, client, admin_headers):
        resp = client.post(
            "/api/v1/rules",
            json=_sample_create_payload(name="dml", user_sql="INSERT INTO logs VALUES (1)"),
            headers=admin_headers,
        )

        _assert_refused(resp)
        assert client.get("/api/v1/rules/dml", headers=admin_headers).status_code == 404

    def test_create_refuses_empty_sql(self, client, admin_headers):
        resp = client.post(
            "/api/v1/rules",
            json=_sample_create_payload(name="empty", user_sql=""),
            headers=admin_headers,
        )

        assert resp.status_code == 422, resp.text
        assert client.get("/api/v1/rules/empty", headers=admin_headers).status_code == 404

    def test_update_refuses_and_keeps_the_stored_rule(self, client, admin_headers):
        payload = _sample_create_payload(name="kept")
        assert client.post("/api/v1/rules", json=payload, headers=admin_headers).status_code == 201

        resp = client.put(
            "/api/v1/rules/kept",
            json=_sample_create_payload(user_sql=_HYPERDX_TEMPLATE_SQL),
            headers=admin_headers,
        )

        _assert_refused(resp)
        stored = client.get("/api/v1/rules/kept", headers=admin_headers).json()
        assert stored["original_sql"] == payload["user_sql"]
        assert stored["sql_errors"] == []

    def test_validate_names_the_missing_database(self, client, admin_headers):
        resp = client.post(
            "/api/v1/rules/validate",
            json={"sql": "SELECT * FROM events WHERE severity = 'high'"},
            headers=admin_headers,
        )

        assert resp.status_code == 200
        assert resp.json()["valid"] is False
        assert any("<db>.<table>" in error["message"] for error in resp.json()["errors"])


# Each shape leaves nothing once the time window is stripped: HyperDX's `_json:*`,
# presence checks on header columns, and conditions that are simply true.
_MATCHES_EVERYTHING = {
    "lucene_star": "notEmpty(toString(`_json`)) = 1",
    "json_present": "notEmpty(_json) = 1",
    "is_not_null": "_json IS NOT NULL",
    "not_blank": "_json <> ''",
    "uuid_present": "isNotNull(_uuid)",
    "one_equals_one": "1 = 1",
    "literal_true": "true",
    "not_zero": "NOT 0",
    "or_tautology": "severity = 'high' OR 1 = 1",
    "presence_pair": "notEmpty(_json) = 1 AND _timestamp_load IS NOT NULL",
}

# Narrow enough for this guard: the volume preview judges how much they match.
_NARROWS = {
    "json_path": "_json.a = 1",
    "negated_value": "severity != 'x'",
    "json_path_present": "notEmpty(toString(`_json`.`user`.`name`)) = 1",
    "presence_and_value": "notEmpty(_json) = 1 AND severity = 'high'",
}


def _assert_matches_everything(resp, where: str) -> str:
    """The rule_matches_everything refusal, returning its message."""
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "rule_matches_everything"
    assert body["message"].startswith("This rule matches every event in dfe.main: ")
    assert body["message"].endswith("Add a condition that narrows it.")
    assert body["context"]["source"] == "dfe.main"
    assert where in body["context"]["where_clause"]
    return body["message"]


class TestRulesRefuseMatchEverything:
    """A rule whose condition matches every event is refused with 422 and never written."""

    @pytest.mark.parametrize("where", _MATCHES_EVERYTHING.values(), ids=_MATCHES_EVERYTHING)
    def test_create_refuses_and_writes_nothing(self, client, admin_headers, where):
        resp = client.post(
            "/api/v1/rules",
            json=_sample_create_payload(
                name="everything", user_sql=f"SELECT * FROM dfe.main WHERE {where}"
            ),
            headers=admin_headers,
        )

        _assert_matches_everything(resp, where)
        assert client.get("/api/v1/rules/everything", headers=admin_headers).status_code == 404

    def test_the_time_window_is_stripped_before_judging(self, client, admin_headers):
        rendered = (
            "SELECT _timestamp,_json FROM dfe.main WHERE (_timestamp >= "
            "fromUnixTimestamp64Milli(1790553600000) AND _timestamp <= "
            "fromUnixTimestamp64Milli(1790557200000)) AND (notEmpty(toString(`_json`)) = 1) "
            "ORDER BY _timestamp DESC LIMIT 200"
        )
        resp = client.post(
            "/api/v1/rules/from-hyperdx",
            json={"raw_sql": rendered, "saved_search_name": "Everything"},
            headers=admin_headers,
        )

        message = _assert_matches_everything(resp, "notEmpty(toString(`_json`)) = 1")
        assert "true whenever _json is present, and every event has it" in message
        assert client.get("/api/v1/rules", headers=admin_headers).json()["total"] == 0

    def test_update_refuses_and_keeps_the_stored_rule(self, client, admin_headers):
        payload = _sample_create_payload(name="kept_narrow")
        assert client.post("/api/v1/rules", json=payload, headers=admin_headers).status_code == 201

        resp = client.put(
            "/api/v1/rules/kept_narrow",
            json=_sample_create_payload(user_sql="SELECT * FROM dfe.main WHERE 1 = 1"),
            headers=admin_headers,
        )

        assert "its condition is always true" in _assert_matches_everything(resp, "1 = 1")
        stored = client.get("/api/v1/rules/kept_narrow", headers=admin_headers).json()
        assert stored["original_sql"] == payload["user_sql"]

    def test_an_empty_filter_keeps_its_own_refusal(self, client, admin_headers):
        resp = client.post(
            "/api/v1/rules",
            json=_sample_create_payload(name="bare", user_sql="SELECT * FROM dfe.main"),
            headers=admin_headers,
        )

        assert [e["message"] for e in _assert_refused(resp)] == [
            "Rule has empty detection logic (WHERE clause)."
        ]

    @pytest.mark.parametrize("where", _NARROWS.values(), ids=_NARROWS)
    def test_a_narrowing_condition_is_saved(self, client, admin_headers, where):
        resp = client.post(
            "/api/v1/rules",
            json=_sample_create_payload(
                name="narrow", user_sql=f"SELECT * FROM dfe.main WHERE {where}"
            ),
            headers=admin_headers,
        )

        assert resp.status_code == 201, resp.text
        assert resp.json()["rule"]["where_clause"] == where


class TestRulesVolumePreviewWiring:
    """Every write route runs the volume preview, and with no ClickHouse here it says so."""

    _UNKNOWN = "The alert-volume preview could not finish: "

    def test_create_reports_the_volume_it_could_not_measure(self, client, admin_headers):
        resp = client.post("/api/v1/rules", json=_sample_create_payload(), headers=admin_headers)

        assert resp.status_code == 201, resp.text
        warnings = resp.json()["rule"]["warnings"]
        assert any(w.startswith(self._UNKNOWN) for w in warnings), warnings
        estimate = resp.json()["cost_estimate"]
        assert estimate["band"] == "unmeasured"
        assert estimate["window_minutes"] == 60

    def test_opting_out_of_the_numbers_still_warns(self, client, admin_headers):
        resp = client.post(
            "/api/v1/rules",
            json=_sample_create_payload(estimate_cost=False, cost_window_minutes=0),
            headers=admin_headers,
        )

        assert resp.status_code == 201, resp.text
        assert resp.json()["cost_estimate"] is None
        assert any(w.startswith(self._UNKNOWN) for w in resp.json()["rule"]["warnings"])

    def test_update_and_from_hyperdx_warn_too(self, client, admin_headers):
        assert (
            client.post(
                "/api/v1/rules", json=_sample_create_payload(), headers=admin_headers
            ).status_code
            == 201
        )
        update = client.put(
            "/api/v1/rules/test_rule", json=_sample_create_payload(), headers=admin_headers
        )
        hyperdx = client.post(
            "/api/v1/rules/from-hyperdx",
            json={"raw_sql": _HYPERDX_RENDERED_SQL},
            headers=admin_headers,
        )

        assert update.status_code == 200, update.text
        assert hyperdx.status_code == 201, hyperdx.text
        assert any(w.startswith(self._UNKNOWN) for w in update.json()["rule"]["warnings"])
        assert any(w.startswith(self._UNKNOWN) for w in hyperdx.json()["warnings"])
