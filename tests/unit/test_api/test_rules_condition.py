#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_rules_condition.py
#  Purpose:      A rule written as a condition: create, update, read back, and the preview
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A rule stores its structured condition beside the SQL it compiles to.

The preview runs the save's own checks and writes nothing, and a hunt list filters
by the rule it runs.
"""

from types import SimpleNamespace

import pytest

from dfe_engine.api.deps import get_rule_registry
from dfe_engine.hunts.rule_creation_service import RuleCreationService
from dfe_engine.hunts.rule_model import Rule

RULES = "/api/v1/rules"
HUNTS = "/api/v1/hunts"
ORGS = "/api/v1/orgs"

_CONDITION = {
    "all": [
        {"field": "user.name", "value": "bob"},
        {"field": "attempts", "operator": "gte", "value": 5},
    ]
}
_COLUMNS = [
    ("_org_id", "String"),
    ("_uuid", "UUID"),
    ("_timestamp", "DateTime64(3)"),
    ("host", "String"),
    ("_json", "JSON"),
]


class _ClickHouse:
    """Reports the source's columns, parses every rule, and counts 100 events with 2 matches."""

    def __init__(self) -> None:
        self.previews: list[str] = []
        self.columns_readable = True

    def command(self, *_args, **_kwargs) -> None:
        """Answer the parse and plan checks."""

    def query(self, sql: str, **_kwargs) -> SimpleNamespace:
        """Answer the column read, and record and count a preview."""
        if "system.columns" in sql:
            if not self.columns_readable:
                raise ConnectionError("clickhouse is down")
            return SimpleNamespace(result_rows=_COLUMNS)
        self.previews.append(sql)
        return SimpleNamespace(result_rows=[(100, 2)])


@pytest.fixture
def clickhouse(monkeypatch) -> _ClickHouse:
    recording = _ClickHouse()
    monkeypatch.setattr(RuleCreationService, "_query_client", lambda _self: recording)
    return recording


def _rule(name: str = "bob_attempts", **fields) -> dict:
    return {"name": name, "severity": "high", "source": "windows-audit", **fields}


def _created(client, headers, **fields):
    resp = client.post(RULES, json=_rule(condition=_CONDITION, **fields), headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestCreateWithACondition:
    def test_the_engine_compiles_it_and_stores_both(self, client, admin_headers, clickhouse):
        created = _created(client, admin_headers)

        rule = created["rule"]
        assert rule["condition"]["all"][0] == {
            "field": "user.name",
            "operator": "equals",
            "value": "bob",
            "negate": False,
            "all": [],
            "any": [],
        }
        assert rule["original_sql"].startswith("SELECT * FROM `dfe`.`windows-audit` WHERE ")
        assert rule["source_db"] == "dfe"
        assert rule["source_table"] == "windows-audit"
        assert "_json.`user`.`name`.:String = 'bob'" in rule["where_clause"]
        assert created["cost_estimate"]["estimated_rows"] == 2

    def test_it_reads_back_as_written(self, client, admin_headers, clickhouse):
        written = _created(client, admin_headers)["rule"]["condition"]

        read = client.get(f"{RULES}/bob_attempts", headers=admin_headers).json()

        assert read["condition"] == written
        assert "attempts" in read["where_clause"]

    def test_the_stored_condition_holds_only_what_was_set(self):
        rule = Rule(
            rule_id="r",
            name="r",
            condition={"all": [{"field": "a", "value": "x"}, {"field": "b", "operator": "exists"}]},
        )

        assert rule.model_dump()["condition"] == {
            "all": [
                {"field": "a", "operator": "equals", "value": "x"},
                {"field": "b", "operator": "exists"},
            ]
        }
        assert Rule.model_validate(rule.model_dump()).condition == rule.condition

    def test_a_field_is_a_column_where_the_table_has_one(self, client, admin_headers, clickhouse):
        body = _rule(condition={"field": "host", "operator": "in", "value": ["a", "b"]})

        resp = client.post(RULES, json=body, headers=admin_headers)

        assert resp.status_code == 201, resp.text
        assert resp.json()["rule"]["where_clause"] == "(`host` IN ('a', 'b'))"

    def test_a_rule_written_as_sql_has_no_condition(self, client, admin_headers):
        sql = "SELECT * FROM dfe.main WHERE severity = 'high'"

        resp = client.post(RULES, json=_rule(user_sql=sql), headers=admin_headers)

        assert resp.status_code == 201, resp.text
        assert resp.json()["rule"]["condition"] is None


class TestUpdateWithACondition:
    def test_a_condition_is_replaced_by_another(self, client, admin_headers, clickhouse):
        _created(client, admin_headers)
        replacement = {"field": "host", "value": "alpha"}

        resp = client.put(
            f"{RULES}/bob_attempts", json=_rule(condition=replacement), headers=admin_headers
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["rule"]["condition"]["field"] == "host"
        assert resp.json()["rule"]["where_clause"] == "(`host` = 'alpha')"

    def test_sql_replaces_a_condition_and_drops_it(self, client, admin_headers, clickhouse):
        _created(client, admin_headers)
        sql = "SELECT * FROM dfe.main WHERE severity = 'high'"

        resp = client.put(f"{RULES}/bob_attempts", json=_rule(user_sql=sql), headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert (
            client.get(f"{RULES}/bob_attempts", headers=admin_headers).json()["condition"] is None
        )

    def test_a_condition_reads_the_source_the_rule_already_has(
        self, client, admin_headers, clickhouse
    ):
        sql = "SELECT * FROM dfe.main WHERE severity = 'high'"
        client.post(RULES, json=_rule("by_sql", user_sql=sql), headers=admin_headers)
        body = {"name": "by_sql", "condition": {"field": "host", "value": "alpha"}}

        resp = client.put(f"{RULES}/by_sql", json=body, headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["rule"]["source_table"] == "windows-audit"


class TestARefusedWriteSavesNothing:
    @pytest.mark.parametrize(
        ("fields", "code", "mentions"),
        [
            (
                {"user_sql": "SELECT 1 FROM dfe.main WHERE x = 1"},
                "validation_error",
                "one of the two",
            ),
            ({"condition": None}, "validation_error", "one of the two"),
            ({"source": None}, "validation_error", "name the source"),
            ({"source": "dfe.windows-audit"}, "invalid_sql", "a label, not a <db>.<table>"),
            ({"source": "a b"}, "invalid_sql", "is not a table label"),
            (
                {"condition": {"field": "_nothing", "value": "x"}},
                "invalid_sql",
                "no column '_nothing'",
            ),
            (
                {"condition": {"field": "host.name", "value": "x"}},
                "invalid_sql",
                "names nothing inside it",
            ),
            ({"condition": {"field": "a b", "value": "x"}}, "validation_error", "not a column"),
            (
                {"condition": {"field": "a", "operator": "gt", "value": "yesterday"}},
                "validation_error",
                "ISO 8601",
            ),
            (
                {"condition": {"field": "_uuid", "operator": "exists"}},
                "rule_matches_everything",
                "every event",
            ),
            (
                {"condition": {"field": "_timestamp", "operator": "gt", "value": "2026-01-01"}},
                "invalid_sql",
                "bounds time for every rule",
            ),
            (
                {
                    "condition": {
                        "all": [
                            {"field": "host", "value": "a"},
                            {"field": "_timestamp", "operator": "lte", "value": 1700000000},
                        ]
                    }
                },
                "invalid_sql",
                "bounds time for every rule",
            ),
            ({"source_type": "hyperdx"}, "validation_error", "source_type is 'raw'"),
            (
                {"condition": {"field": "a", "value": "{window}"}},
                "validation_error",
                "which the runner fills in",
            ),
        ],
        ids=[
            "sql and condition",
            "neither",
            "no source",
            "source with a database",
            "source that is no label",
            "unknown header column",
            "path inside a plain column",
            "field that is no path",
            "date that is no date",
            "condition that matches everything",
            "time column against a date",
            "time column against a number in a group",
            "hyperdx source type",
            "operand carrying a runner placeholder",
        ],
    )
    def test_create(self, client, admin_headers, clickhouse, fields, code, mentions):
        body = _rule(condition=_CONDITION)
        body.update(fields)

        resp = client.post(RULES, json=body, headers=admin_headers)

        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == code
        assert mentions in resp.text
        assert client.get(f"{RULES}/bob_attempts", headers=admin_headers).status_code == 404

    def test_a_condition_is_not_guessed_when_the_columns_cannot_be_read(
        self, client, admin_headers, clickhouse
    ):
        clickhouse.columns_readable = False

        resp = client.post(RULES, json=_rule(condition=_CONDITION), headers=admin_headers)

        assert resp.status_code == 422, resp.text
        assert "ClickHouse could not be read for the columns of dfe.windows-audit" in resp.text
        assert client.get(f"{RULES}/bob_attempts", headers=admin_headers).status_code == 404

    def test_an_update_keeps_the_stored_rule(self, client, admin_headers, clickhouse):
        before = _created(client, admin_headers)["rule"]
        body = _rule(condition={"field": "_nothing", "value": "x"})

        resp = client.put(f"{RULES}/bob_attempts", json=body, headers=admin_headers)

        assert resp.status_code == 422, resp.text
        assert (
            client.get(f"{RULES}/bob_attempts", headers=admin_headers).json()["condition"]
            == (before["condition"])
        )

    def test_a_condition_with_no_source_anywhere_is_refused_on_update(
        self, client, admin_headers, clickhouse
    ):
        get_rule_registry().save(
            Rule(rule_id="bare", name="bare", where_clause="x = 1", source_table="main")
        )

        resp = client.put(
            f"{RULES}/bare",
            json={"name": "bare", "condition": {"field": "host", "value": "a"}},
            headers=admin_headers,
        )

        assert resp.status_code == 422, resp.text
        assert "name the source" in resp.text


class TestPreview:
    def test_it_answers_what_a_save_would_and_writes_nothing(
        self, client, admin_headers, clickhouse
    ):
        resp = client.post(
            f"{RULES}/preview",
            json={"source": "windows-audit", "condition": _CONDITION},
            headers=admin_headers,
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["user_sql"].startswith("SELECT * FROM `dfe`.`windows-audit` WHERE ")
        assert (body["source_db"], body["source_table"]) == ("dfe", "windows-audit")
        assert "_json.`user`.`name`.:String = 'bob'" in body["where_clause"]
        assert body["sql_errors"] == []
        assert body["matches_everything"] is None
        assert body["cost_estimate"]["estimated_rows"] == 2
        assert client.get(RULES, headers=admin_headers).json()["items"] == []

    def test_it_compiles_what_a_save_stores(self, client, admin_headers, clickhouse):
        previewed = client.post(
            f"{RULES}/preview",
            json={"source": "windows-audit", "condition": _CONDITION},
            headers=admin_headers,
        ).json()

        saved = _created(client, admin_headers)["rule"]

        assert previewed["user_sql"] == saved["original_sql"]
        assert previewed["where_clause"] == saved["where_clause"]

    def test_a_cel_filter_is_anded_in_as_on_save(self, client, admin_headers, clickhouse):
        body = {
            "source": "windows-audit",
            "condition": {"field": "host", "value": "a"},
            "cel_filter": "amount > 10",
        }

        previewed = client.post(f"{RULES}/preview", json=body, headers=admin_headers).json()

        assert previewed["where_clause"] == "((`host` = 'a')) AND (amount > 10)"
        assert previewed["sql_errors"] == []

    @pytest.mark.parametrize(
        ("body", "in_errors"),
        [
            ({"source": "windows-audit", "condition": {"field": "_nothing", "value": "x"}}, True),
            ({"source": "dfe.windows-audit", "condition": {"field": "a", "value": "x"}}, True),
            (
                {
                    "source": "windows-audit",
                    "condition": {"field": "a", "value": "x"},
                    "cel_filter": "amount >",
                },
                True,
            ),
        ],
        ids=["unknown column", "source with a database", "cel that does not parse"],
    )
    def test_a_condition_a_save_refuses_is_answered_with_its_reasons(
        self, client, admin_headers, clickhouse, body, in_errors
    ):
        resp = client.post(f"{RULES}/preview", json=body, headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert bool(resp.json()["sql_errors"]) is in_errors
        assert resp.json()["cost_estimate"] is None

    def test_a_condition_that_matches_everything_says_why(self, client, admin_headers, clickhouse):
        body = {"source": "windows-audit", "condition": {"field": "_uuid", "operator": "exists"}}

        resp = client.post(f"{RULES}/preview", json=body, headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert "every event" in resp.json()["matches_everything"]
        assert resp.json()["cost_estimate"] is None

    def test_a_time_bound_a_save_would_drop_is_reported_not_previewed(
        self, client, admin_headers, clickhouse
    ):
        body = {
            "source": "windows-audit",
            "condition": {"field": "_timestamp", "operator": "gt", "value": "2026-01-01"},
        }

        resp = client.post(f"{RULES}/preview", json=body, headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert "bounds time for every rule" in resp.json()["sql_errors"][0]["message"]
        assert resp.json()["cost_estimate"] is None

    @pytest.mark.parametrize(
        "body",
        [
            {"source": "windows-audit"},
            {"condition": {"field": "a", "value": "x"}},
            {
                "source": "windows-audit",
                "condition": {"field": "a", "operator": "regex", "value": "x"},
            },
            {"source": "windows-audit", "condition": {"field": "a", "not": True, "value": "x"}},
            {"source": "windows-audit", "condition": {"field": "a b", "value": "x"}},
            {"source": "windows-audit", "condition": {"all": []}},
        ],
        ids=[
            "no condition",
            "no source",
            "operator no rule takes",
            "unknown key",
            "field that is no path",
            "empty group",
        ],
    )
    def test_a_body_that_is_no_condition_is_422(self, client, admin_headers, body):
        resp = client.post(f"{RULES}/preview", json=body, headers=admin_headers)

        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "validation_error"

    def test_it_counts_the_orgs_of_the_hunts_that_run_the_rule(
        self, client, admin_headers, clickhouse
    ):
        org = client.post(ORGS, json={"name": "acme", "org_ids": ["acme-1"]}, headers=admin_headers)
        assert org.status_code == 201, org.text
        hunt = {
            "name": "windows_hunt",
            "cron": "*/5 * * * *",
            "rules": ["bob_attempts"],
            "global_source_table_name": "dfe.windows-audit",
            "global_target_table_name": "dfe.detection",
            "customers": ["acme"],
        }
        assert client.post(HUNTS, json=hunt, headers=admin_headers).status_code == 201

        def counted(**extra) -> int:
            clickhouse.previews.clear()
            body = {"source": "windows-audit", "condition": _CONDITION, **extra}
            assert (
                client.post(f"{RULES}/preview", json=body, headers=admin_headers).status_code == 200
            )
            return sum(sql.count("_org_id IN ('acme-1')") for sql in clickhouse.previews)

        assert counted(name="bob_attempts") == 2
        assert counted(hunt_name="windows_hunt") == 2
        assert counted() == 0

    def test_an_edit_counts_the_hunt_the_stored_rule_names(self, client, admin_headers, clickhouse):
        org = client.post(ORGS, json={"name": "acme", "org_ids": ["acme-1"]}, headers=admin_headers)
        assert org.status_code == 201, org.text
        hunt = {
            "name": "windows_hunt",
            "cron": "*/5 * * * *",
            "rules": ["another_rule"],
            "global_source_table_name": "dfe.windows-audit",
            "global_target_table_name": "dfe.detection",
            "customers": ["acme"],
        }
        assert client.post(HUNTS, json=hunt, headers=admin_headers).status_code == 201
        _created(client, admin_headers, hunt_name="windows_hunt")
        clickhouse.previews.clear()

        body = {"source": "windows-audit", "condition": _CONDITION, "name": "bob_attempts"}
        assert client.post(f"{RULES}/preview", json=body, headers=admin_headers).status_code == 200
        preview_sql = clickhouse.previews[-1]
        clickhouse.previews.clear()
        update = _rule(condition=_CONDITION)
        update.pop("name")
        assert (
            client.put(f"{RULES}/bob_attempts", json=update, headers=admin_headers).status_code
            == 200
        )

        assert preview_sql.count("_org_id IN ('acme-1')") == 2
        assert preview_sql == clickhouse.previews[-1]


class TestHuntsByRule:
    @pytest.fixture
    def hunts(self, client, admin_headers) -> None:
        org = client.post(ORGS, json={"name": "acme", "org_ids": ["acme-1"]}, headers=admin_headers)
        assert org.status_code == 201, org.text
        base = {
            "cron": "*/5 * * * *",
            "customers": ["acme"],
            "global_target_table_name": "dfe.detection",
        }
        for name, rules in {
            "hunt_a": ["alpha", "beta"],
            "hunt_b": ["beta"],
            "hunt_c": ["alpha_two"],
        }.items():
            resp = client.post(
                HUNTS,
                json={**base, "name": name, "rules": rules, "global_source_table_name": "dfe.main"},
                headers=admin_headers,
            )
            assert resp.status_code == 201, resp.text

    @pytest.mark.parametrize(
        ("rule", "expected"),
        [
            ("alpha", ["hunt_a"]),
            ("beta", ["hunt_a", "hunt_b"]),
            ("alpha_two", ["hunt_c"]),
            ("alp", []),
            ("ALPHA", []),
            ("nothing", []),
            ("", []),
        ],
    )
    def test_the_list_keeps_the_hunts_that_run_exactly_that_rule(
        self, client, admin_headers, hunts, rule, expected
    ):
        resp = client.get(HUNTS, params={"rule": rule}, headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert sorted(item["name"] for item in resp.json()["items"]) == expected

    def test_without_the_filter_every_hunt_is_listed(self, client, admin_headers, hunts):
        resp = client.get(HUNTS, headers=admin_headers)

        assert len(resp.json()["items"]) == 3
