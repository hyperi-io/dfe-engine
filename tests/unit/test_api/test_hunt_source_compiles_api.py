#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_hunt_source_compiles_api.py
#  Purpose:      A hunt the API saves over a hyphenated or bare source is one the runner compiles
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""From ``POST /api/v1/hunts`` to the statement the hunt runner would run.

A source table is named for its source label, such as ``cisco-ios``, so the API
accepts it and the runner backtick-quotes it into ``FROM``. A source named with no
database is read from the data database, the one the runner's CLI resolves.
"""

from pathlib import Path

from dfe_engine.hunt_runner import cli
from dfe_engine.hunt_runner.spec_loader import load_specs
from dfe_engine.hunts.rule_model import Rule
from dfe_engine.hunts.rule_registry import RuleRegistry

_HUNT = {
    "display_name": "Certutil Abuse",
    "cron": "*/5 * * * *",
    "customers": ["org_a"],
    "rules": ["certutil"],
}


def _runner_specs(settings) -> dict:
    database = settings.clickhouse.effective_data_database
    return load_specs(settings.hunts.hunt_dir, **cli._spec_sources(settings, database))


def test_a_hunt_over_a_hyphenated_source_saves_and_compiles(client, admin_headers, api_settings):
    rule = client.post(
        "/api/v1/rules",
        json={
            "name": "certutil",
            "user_sql": "SELECT * FROM dfe.`cisco-ios` WHERE process_name = 'certutil.exe'",
            "source": "cisco-ios",
        },
        headers=admin_headers,
    )
    assert rule.status_code == 201, rule.text

    hunt = client.post(
        "/api/v1/hunts",
        json={"name": "cisco_hunt", **_HUNT, "global_source_table_name": "dfe.cisco-ios"},
        headers=admin_headers,
    )
    assert hunt.status_code == 201, hunt.text

    [statement] = _runner_specs(api_settings)["cisco_hunt"].queries
    assert "\nFROM `dfe`.`cisco-ios`\n" in statement.sql
    assert "process_name = 'certutil.exe'" in statement.sql


def test_a_hunt_over_a_bare_source_compiles_against_the_data_database(
    client, admin_headers, api_settings
):
    registry = RuleRegistry(
        rules_directory=Path(api_settings.hunts.rules_dir), writable=True, refresh_interval=0
    )
    try:
        registry.save(Rule(rule_id="certutil", name="Certutil", where_clause="a = 1"))
    finally:
        registry.close()

    hunt = client.post(
        "/api/v1/hunts",
        json={"name": "bare_hunt", **_HUNT, "global_source_table_name": "main"},
        headers=admin_headers,
    )
    assert hunt.status_code == 201, hunt.text

    database = api_settings.clickhouse.effective_data_database
    [statement] = _runner_specs(api_settings)["bare_hunt"].queries
    assert f"\nFROM `{database}`.`main`\n" in statement.sql
    assert statement.sql.startswith(f"INSERT INTO `{database}`.`detection`\n")
