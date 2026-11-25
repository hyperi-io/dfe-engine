#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_hunts_edit_overrides.py
#  Purpose:      An API edit of a hunt keeps the per-rule overrides its stored YAML holds
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A hunt written straight to the deploy repo can give a rule its own source and target.

The API takes rules by name only, so an edit through it keeps the stored entry of each
rule that stays in the hunt. A rule the edit drops goes with its entry, and a rule it
adds is stored by name.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from common.hunt_files import store_rule_file

from dfe_engine.api.deps import get_hunt_config_registry
from dfe_engine.hunts.hunt_output import HuntResultSchema
from dfe_engine.hunts.rule_compiler import rule_tables
from dfe_engine.hunts.rule_creation_service import RuleCreationService

HUNTS = "/api/v1/hunts"
_HUNT = {
    "cron": "*/5 * * * *",
    "customers": ["org_a"],
    "global_source_table_name": "dfe.main",
    "global_target_table_name": "dfe.detection",
}
_KEPT = {
    "rule_name": "certutil",
    "source_table_name": "dfe.winaudit",
    "target_table_name": "dfe.windows_detections",
}
_DROPPED = {"rule_name": "netcat", "source_table_name": "dfe.linux"}


@pytest.fixture
def hunt_with_overrides(client, admin_headers, api_settings, customer_orgs) -> None:
    """A hunt whose stored YAML gives two rules their own source and target."""
    rules_dir = Path(api_settings.hunts.rules_dir)
    for name in ("certutil", "netcat", "added"):
        store_rule_file(rules_dir, name)
    created = client.post(
        HUNTS,
        json={"name": "windows_hunt", **_HUNT, "rules": ["certutil", "netcat"]},
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    registry = get_hunt_config_registry()
    stored = registry.get("windows_hunt")
    registry.save("windows_hunt", {**stored, "rules": [dict(_KEPT), dict(_DROPPED)]})


@pytest.mark.usefixtures("hunt_with_overrides")
@pytest.mark.parametrize(
    ("rules", "expected"),
    [
        pytest.param(
            ["certutil", "netcat"], [_KEPT, _DROPPED], id="every rule stays, every entry stays"
        ),
        pytest.param(["certutil"], [_KEPT], id="a dropped rule goes with its entry"),
        pytest.param(
            ["certutil", "added"],
            [_KEPT, {"rule_name": "added"}],
            id="an added rule is stored by name",
        ),
    ],
)
def test_an_edit_keeps_the_entry_of_each_rule_that_stays(client, admin_headers, rules, expected):
    resp = client.put(
        f"{HUNTS}/windows_hunt",
        json={**_HUNT, "cron": "*/10 * * * *", "rules": rules},
        headers=admin_headers,
    )

    assert resp.status_code == 200, resp.text
    stored = get_hunt_config_registry().get("windows_hunt")
    assert stored["cron"] == "*/10 * * * *"
    assert stored["rules"] == expected


class _ClickHouse:
    """Holds only the tables it names, each with every column the runner inserts."""

    def __init__(self, tables: set[tuple[str, str]]) -> None:
        self.tables = tables

    def command(self, *_args, **_kwargs) -> str:
        """Plan every rule."""
        return ""

    def query(self, _sql: str, parameters: dict | None = None, **_kwargs) -> SimpleNamespace:
        """Answer the ``system.columns`` read for the table the parameters name."""
        named = parameters or {}
        held = (named["db"], named["tbl"]) in self.tables
        columns = HuntResultSchema().insert_columns() if held else []
        return SimpleNamespace(result_rows=[(name, "String") for name in columns])


@pytest.mark.usefixtures("hunt_with_overrides")
@pytest.mark.parametrize(
    ("tables", "status"),
    [
        pytest.param(
            {("dfe", "windows_detections"), ("dfe", "detection")},
            200,
            id="the kept target exists",
        ),
        pytest.param({("dfe", "detection")}, 422, id="the kept target is missing"),
    ],
)
def test_the_save_checks_the_kept_override_target(
    client, admin_headers, monkeypatch, tables, status
):
    stub = _ClickHouse(tables)
    monkeypatch.setattr(RuleCreationService, "_query_client", lambda _self: stub)

    resp = client.put(
        f"{HUNTS}/windows_hunt",
        json={**_HUNT, "cron": "*/10 * * * *", "rules": ["certutil"]},
        headers=admin_headers,
    )

    assert resp.status_code == status, resp.text
    if status == 422:
        assert resp.json()["code"] == "invalid_target"
        assert "dfe.windows_detections" in resp.json()["detail"]


def _entry(detail: dict, rule: str) -> dict:
    (entry,) = [entry for entry in detail["rules"] if entry["rule_name"] == rule]
    return entry


@pytest.mark.usefixtures("hunt_with_overrides")
class TestTheSourceOverrideIsOneValue:
    """The stored ``source_table_name`` is what GET shows as ``source`` and what the runner reads."""

    def test_get_shows_the_stored_source(self, client, admin_headers):
        detail = client.get(f"{HUNTS}/windows_hunt", headers=admin_headers).json()

        assert _entry(detail, "certutil")["source"] == "dfe.winaudit"
        assert _entry(detail, "certutil")["target_table_name"] == "dfe.windows_detections"

    def test_an_edit_that_leaves_it_alone_shows_it_after(self, client, admin_headers):
        resp = client.put(
            f"{HUNTS}/windows_hunt",
            json={**_HUNT, "cron": "*/10 * * * *", "rules": ["certutil"]},
            headers=admin_headers,
        )

        assert _entry(resp.json(), "certutil")["source"] == "dfe.winaudit"
        stored = get_hunt_config_registry().get("windows_hunt")
        assert stored["rules"][0]["source_table_name"] == "dfe.winaudit"

    def test_the_runner_reads_the_value_get_shows(self, client, admin_headers):
        detail = client.get(f"{HUNTS}/windows_hunt", headers=admin_headers).json()
        stored = get_hunt_config_registry().get("windows_hunt")

        tables = rule_tables(
            stored["rules"][0],
            "",
            "",
            hunt_source=stored["global_source_table_name"],
            hunt_target=stored["global_target_table_name"],
            default_target="dfe.detection",
            default_database="dfe",
        )

        shown = _entry(detail, "certutil")["source"]
        assert f"{tables.source_db}.{tables.source_table}" == shown
