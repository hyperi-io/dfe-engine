#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_hunt_write_checks.py
#  Purpose:      A hunt write checks each rule's query and its results table
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""POST and PUT /api/v1/hunts refuse a hunt the runner could not run.

Each rule the hunt names passes the query check a rule save runs. The results table is
held to the columns the runner's INSERT names, read from ``system.columns``. The ClickHouse
here answers the column read and the plan, and nothing else.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from common.hunt_files import store_rule_file
from scalo.api import ProblemError

from dfe_engine.api.v1.hunts import _check_hunt_will_run
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
        self.plan_refused_table: str | None = None
        self.unreadable = False

    def command(self, cmd: str, *_args, **_kwargs) -> str:
        """Answer EXPLAIN PLAN, refusing it when ``plan_error`` is set.

        With ``plan_refused_table`` set, only a plan of that table is refused.
        """
        refused = self.plan_refused_table is None or self.plan_refused_table in cmd
        if cmd.startswith("EXPLAIN PLAN") and self.plan_error is not None and refused:
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

    def test_another_target_is_checked_too(self, client, admin_headers, clickhouse):
        clickhouse.tables[("dfe", "logs_alerts")] = ["rule_id"]
        body = _hunt(global_target_table_name="logs_alerts")

        resp = client.post(HUNTS, json=body, headers=admin_headers)

        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "invalid_target"
        assert "dfe.logs_alerts lacks the columns" in resp.json()["detail"]

    @pytest.mark.parametrize(
        "target",
        [
            pytest.param(None, id="no target, the default"),
            pytest.param("detection", id="bare core table"),
            pytest.param("logs_alerts", id="another table"),
            pytest.param("dfe.logs_alerts", id="another table, qualified"),
        ],
    )
    def test_a_target_that_takes_the_insert_is_saved_with_no_extra_flag(
        self, client, admin_headers, clickhouse, target
    ):
        """The console's hunt form posts the target as typed and nothing else."""
        clickhouse.tables[("dfe", "logs_alerts")] = _INSERT_COLUMNS
        body = _hunt() if target is None else _hunt(global_target_table_name=target)

        resp = client.post(HUNTS, json=body, headers=admin_headers)

        assert resp.status_code == 201, resp.text

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


def _plant_rule(api_settings, name: str, source: str | None) -> None:
    """Store a rule file as the deploy repo holds one, reading *source* when it names one."""
    rules_dir = Path(api_settings.hunts.rules_dir)
    rules_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"display_name: {name}", "where_clause: severity = 'high'"]
    if source is not None:
        database, table = source.split(".")
        lines += [f"source_db: {database}", f"source_table: {table}"]
    (rules_dir / f"{name}.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _yaml_hunt(entry: dict, **changes) -> dict:
    """A stored hunt config as a YAML hunt holds it, with one per-rule entry."""
    return {**_hunt(**changes), "rules": [entry]}


class TestEachRuleIsCheckedWhereTheRunnerReadsAndWrites:
    """The save checks each rule's own source and target, as the runner resolves them."""

    def test_a_per_rule_source_is_read_in_place_of_a_broken_global_one(
        self, client, clickhouse, api_settings
    ):
        _plant_rule(api_settings, "unsourced", None)
        clickhouse.plan_error = _UNKNOWN_COLUMN
        clickhouse.plan_refused_table = "broken"
        config = _yaml_hunt(
            {"rule_name": "unsourced", "source_table_name": "dfe.main"},
            global_source_table_name="dfe.broken",
        )

        _check_hunt_will_run(config, api_settings, None)

    def test_a_per_rule_target_is_checked_in_place_of_a_missing_default(
        self, client, clickhouse, api_settings
    ):
        _plant_rule(api_settings, "targeted", "dfe.main")
        clickhouse.tables = {("dfe", "alerts"): _INSERT_COLUMNS}

        _check_hunt_will_run(
            _yaml_hunt({"rule_name": "targeted", "target_table_name": "alerts"}),
            api_settings,
            None,
        )

    def test_a_broken_per_rule_target_is_refused(self, client, clickhouse, api_settings):
        _plant_rule(api_settings, "targeted", "dfe.main")

        with pytest.raises(ProblemError) as refused:
            _check_hunt_will_run(
                _yaml_hunt({"rule_name": "targeted", "target_table_name": "missing"}),
                api_settings,
                None,
            )

        assert refused.value.status == 422
        assert "dfe.missing does not exist" in str(refused.value)

    def test_an_unqualified_target_is_checked_beside_the_rule_source(
        self, client, admin_headers, clickhouse, api_settings
    ):
        _plant_rule(api_settings, "elsewhere", "other.events")
        clickhouse.tables = {("other", "alerts"): _INSERT_COLUMNS}

        resp = client.post(
            HUNTS,
            json=_hunt(rules=["elsewhere"], global_target_table_name="alerts"),
            headers=admin_headers,
        )

        assert resp.status_code == 201, resp.text
