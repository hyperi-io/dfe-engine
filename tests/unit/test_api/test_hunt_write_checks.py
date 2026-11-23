#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_hunt_write_checks.py
#  Purpose:      A hunt write checks each rule's query, its results table, and a non-default target
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""POST and PUT /api/v1/hunts refuse a hunt the runner could not run.

Each rule the hunt names passes the query check a rule save runs. The results table is
held to the columns the runner's INSERT names, read from ``system.columns``. A target other
than ``<data database>.detection`` needs ``confirm=true``. The ClickHouse here answers
the column read and the plan, and nothing else.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from common.hunt_files import store_rule_file

from dfe_engine.hunts.hunt_output import HuntResultSchema
from dfe_engine.hunts.rule_creation_service import RuleCreationService

pytestmark = pytest.mark.usefixtures("customer_orgs", "certutil_rule")

HUNTS = "/api/v1/hunts"
_HUNT = {
    "name": "windows_hunt",
    "cron": "*/5 * * * *",
    "customers": ["org_a"],
    "rules": ["certutil"],
    "global_source_table_name": "dfe.main",
}
_INSERT_COLUMNS = HuntResultSchema().insert_columns()
_UNKNOWN_COLUMN = Exception(
    "Code: 47. DB::Exception: Unknown expression identifier `nope` in scope "
    "SELECT 1 FROM dfe.main WHERE nope = 1. (UNKNOWN_IDENTIFIER)"
)


class _ClickHouse:
    """Reports the columns each table holds, and plans every rule unless told to refuse one."""

    def __init__(self) -> None:
        self.tables: dict[tuple[str, str], list[str]] = {("dfe", "detection"): _INSERT_COLUMNS}
        self.plan_error: Exception | None = None
        self.unreadable = False

    def command(self, cmd: str, *_args, **_kwargs) -> str:
        """Answer EXPLAIN PLAN, refusing it when ``plan_error`` is set."""
        if cmd.startswith("EXPLAIN PLAN") and self.plan_error is not None:
            raise self.plan_error
        return ""

    def query(self, _sql: str, parameters: dict | None = None, **_kwargs) -> SimpleNamespace:
        """Answer the ``system.columns`` read for the table the parameters name."""
        if self.unreadable:
            raise ConnectionError("clickhouse is down")
        named = parameters or {}
        columns = self.tables.get((named["db"], named["tbl"]), [])
        return SimpleNamespace(result_rows=[(name, "String") for name in columns])


@pytest.fixture
def clickhouse(monkeypatch) -> _ClickHouse:
    stub = _ClickHouse()
    monkeypatch.setattr(RuleCreationService, "_query_client", lambda _self: stub)
    return stub


def _hunt(**changes) -> dict:
    return {**_HUNT, **changes}


def _stored(client, headers) -> list[str]:
    return [row["name"] for row in client.get(HUNTS, headers=headers).json()["items"]]


class TestANonDefaultTargetNeedsConfirmation:
    """The hunt writes detections where an operator never looked, so the write says so first."""

    @pytest.mark.parametrize(
        "target",
        [
            pytest.param(None, id="no target, the default"),
            pytest.param("detection", id="bare core table"),
            pytest.param("dfe.detection", id="qualified core table"),
        ],
    )
    def test_the_core_detection_table_needs_none(self, client, admin_headers, target):
        body = _hunt() if target is None else _hunt(global_target_table_name=target)

        resp = client.post(HUNTS, json=body, headers=admin_headers)

        assert resp.status_code == 201, resp.text

    @pytest.mark.parametrize(
        "target",
        [
            pytest.param("logs_alerts", id="another table"),
            pytest.param("dfe.alerts", id="another table, qualified"),
            pytest.param("other.detection", id="the core name in another database"),
        ],
    )
    def test_any_other_target_is_a_400_until_confirmed(self, client, admin_headers, target):
        body = _hunt(global_target_table_name=target)

        refused = client.post(HUNTS, json=body, headers=admin_headers)
        confirmed = client.post(HUNTS, json=body, params={"confirm": "true"}, headers=admin_headers)

        assert refused.status_code == 400, refused.text
        assert refused.json()["code"] == "confirmation_required"
        assert "confirm=true" in refused.json()["detail"]
        assert confirmed.status_code == 201, confirmed.text
        assert _stored(client, admin_headers) == ["windows_hunt"]

    def test_a_refused_create_writes_nothing(self, client, admin_headers):
        client.post(
            HUNTS, json=_hunt(global_target_table_name="logs_alerts"), headers=admin_headers
        )

        assert _stored(client, admin_headers) == []

    def test_an_update_to_another_target_is_refused_and_keeps_the_stored_hunt(
        self, client, admin_headers
    ):
        assert client.post(HUNTS, json=_hunt(), headers=admin_headers).status_code == 201
        body = {
            k: v for k, v in _hunt(global_target_table_name="logs_alerts").items() if k != "name"
        }

        refused = client.put(f"{HUNTS}/windows_hunt", json=body, headers=admin_headers)
        confirmed = client.put(
            f"{HUNTS}/windows_hunt", json=body, params={"confirm": "true"}, headers=admin_headers
        )

        assert refused.status_code == 400, refused.text
        assert refused.json()["code"] == "confirmation_required"
        assert confirmed.status_code == 200, confirmed.text
        stored = client.get(f"{HUNTS}/windows_hunt", headers=admin_headers).json()
        assert stored["global_target_table_name"] == "logs_alerts"


class TestTheResultsTableTakesWhatTheRunnerInserts:
    @pytest.mark.parametrize(
        ("tables", "fragment"),
        [
            pytest.param(
                {("dfe", "detection"): _INSERT_COLUMNS[:-2]},
                f"lacks the columns the runner writes: {', '.join(_INSERT_COLUMNS[-2:])}",
                id="columns missing",
            ),
            pytest.param({}, "dfe.detection does not exist", id="table absent"),
        ],
    )
    def test_a_table_the_insert_does_not_fit_is_refused(
        self, client, admin_headers, clickhouse, tables, fragment
    ):
        clickhouse.tables = tables

        resp = client.post(HUNTS, json=_hunt(), headers=admin_headers)

        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "invalid_target"
        assert fragment in resp.json()["detail"]
        assert _stored(client, admin_headers) == []

    def test_a_table_with_every_column_is_saved(self, client, admin_headers, clickhouse):
        resp = client.post(HUNTS, json=_hunt(), headers=admin_headers)

        assert resp.status_code == 201, resp.text

    def test_a_confirmed_other_target_is_checked_too(self, client, admin_headers, clickhouse):
        clickhouse.tables[("dfe", "logs_alerts")] = ["rule_id"]
        body = _hunt(global_target_table_name="logs_alerts")

        resp = client.post(HUNTS, json=body, params={"confirm": "true"}, headers=admin_headers)

        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "invalid_target"
        assert "dfe.logs_alerts lacks the columns" in resp.json()["detail"]

    def test_an_unreadable_clickhouse_leaves_the_table_unchecked(
        self, client, admin_headers, clickhouse
    ):
        clickhouse.unreadable = True

        resp = client.post(HUNTS, json=_hunt(), headers=admin_headers)

        assert resp.status_code == 201, resp.text

    def test_an_update_is_checked_too(self, client, admin_headers, clickhouse):
        assert client.post(HUNTS, json=_hunt(), headers=admin_headers).status_code == 201
        clickhouse.tables = {}
        body = {k: v for k, v in _hunt().items() if k != "name"}

        resp = client.put(f"{HUNTS}/windows_hunt", json=body, headers=admin_headers)

        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "invalid_target"


class TestEachRuleHasItsQueryChecked:
    def test_a_rule_clickhouse_cannot_plan_refuses_the_hunt(
        self, client, admin_headers, clickhouse
    ):
        clickhouse.plan_error = _UNKNOWN_COLUMN

        resp = client.post(HUNTS, json=_hunt(), headers=admin_headers)

        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert body["code"] == "invalid_sql"
        assert "rule 'certutil': ClickHouse cannot plan this rule" in body["detail"]
        assert body["sql_errors"][0]["message"].startswith("rule 'certutil': ")
        assert _stored(client, admin_headers) == []

    def test_a_rule_that_plans_is_saved(self, client, admin_headers, clickhouse):
        resp = client.post(HUNTS, json=_hunt(), headers=admin_headers)

        assert resp.status_code == 201, resp.text

    def test_a_rule_reading_outside_the_row_refuses_the_hunt_without_clickhouse(
        self, client, admin_headers, api_settings
    ):
        rules_dir = Path(api_settings.hunts.rules_dir)
        (rules_dir / "leaky.yaml").write_text(
            "display_name: leaky\nwhere_clause: url('http://203.0.113.9/') = 1\n",
            encoding="utf-8",
            newline="\n",
        )

        resp = client.post(HUNTS, json=_hunt(rules=["leaky"]), headers=admin_headers)

        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "invalid_sql"
        assert "rule 'leaky'" in resp.json()["detail"]

    def test_a_rule_with_no_file_yet_is_left_to_the_existing_check(self, client, admin_headers):
        resp = client.post(HUNTS, json=_hunt(rules=["not_stored"]), headers=admin_headers)

        assert resp.status_code == 422
        assert resp.json()["code"] == "unknown_rule"

    def test_every_rule_is_named_in_the_refusal(
        self, client, admin_headers, clickhouse, api_settings
    ):
        store_rule_file(Path(api_settings.hunts.rules_dir), "second")
        clickhouse.plan_error = _UNKNOWN_COLUMN

        resp = client.post(HUNTS, json=_hunt(rules=["certutil", "second"]), headers=admin_headers)

        messages = [error["message"] for error in resp.json()["sql_errors"]]
        assert [m.split(":")[0] for m in messages] == ["rule 'certutil'", "rule 'second'"]
