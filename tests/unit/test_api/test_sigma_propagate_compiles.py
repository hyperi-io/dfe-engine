#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_sigma_propagate_compiles.py
#  Purpose:      A hunt sigma propagation writes is one the hunt runner compiles
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""POST /sigma/propagate writes a rule and hunt over ``<data db>.{source}_sigma``.

The hunt runner reads a source as ``database.table``. A propagated rule that named
the view with no database, over a source whose label carries ``-``, compiled to
nothing, so every propagated hunt ran empty. No live ClickHouse.
"""

from dfe_engine.api.deps import _registries
from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.hunt_runner import cli
from dfe_engine.hunt_runner.spec_loader import load_specs

_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
_BINDING = f"sigma_windows-audit_{_ID.replace('-', '')}"
_HUNT = "sigma_hunt_windows-audit"

_RULE_YAML = f"""title: Suspicious Certutil
id: {_ID}
status: experimental
logsource:
    category: process_creation
    product: windows
detection:
    selection:
        Image|endswith: \\certutil.exe
    condition: selection
level: high
date: 2022-01-01
modified: 2023-05-01
"""


def _propagate(client, app, headers, tmp_path) -> None:
    app.state.gitcrud = GitCrud(
        GitopsRepo(local_path=str(tmp_path / "deploy"), push=False), default_registry()
    )
    _registries["source"].save_source(
        {
            "source": "windows-audit",
            "enabled": True,
            "match": {"field": "tags.collector.type", "value": "windows-audit"},
            "schema": {"engine": "MergeTree"},
            "views": [{"standard": "sigma", "taxonomy": "windows"}],
        }
    )
    import_dir = tmp_path / "import"
    import_dir.mkdir()
    (import_dir / "a.yml").write_text(_RULE_YAML, encoding="utf-8")
    client.post(
        "/api/v1/sigma/providers",
        json={"name": "files", "kind": "local_files", "options": {"directory": str(import_dir)}},
        headers=headers,
    )
    synced = client.post("/api/v1/sigma/providers/files/sync?wait=30", headers=headers)
    assert synced.status_code == 200, synced.text
    selected = client.post(f"/api/v1/sigma/catalogue/{_ID}/select", headers=headers)
    assert selected.status_code == 200, selected.text
    resp = client.post("/api/v1/sigma/propagate", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["report"]["created"] == [_BINDING]


def test_the_propagated_rule_and_hunt_name_the_data_database(
    client, app, admin_headers, api_settings, tmp_path
):
    _propagate(client, app, admin_headers, tmp_path)
    database = api_settings.clickhouse.effective_data_database

    rule = client.get(f"/api/v1/rules/{_BINDING}", headers=admin_headers).json()
    assert rule["source_db"] == database
    assert rule["source_table"] == "windows-audit_sigma"
    hunt = client.get(f"/api/v1/hunts/{_HUNT}", headers=admin_headers).json()
    assert hunt["global_source_table_name"] == f"{database}.windows-audit_sigma"


def test_a_propagated_hunt_compiles_over_its_sigma_view(
    client, app, admin_headers, api_settings, tmp_path
):
    _propagate(client, app, admin_headers, tmp_path)
    database = api_settings.clickhouse.effective_data_database

    specs = load_specs(api_settings.hunts.hunt_dir, **cli._spec_sources(api_settings, database))

    [statement] = specs[_HUNT].queries
    assert statement.rule_id == _BINDING
    assert f"\nFROM `{database}`.`windows-audit_sigma`\n" in statement.sql
    assert "Image ILIKE" in statement.sql
    assert statement.sql.startswith(f"INSERT INTO `{database}`.`detection`\n")
