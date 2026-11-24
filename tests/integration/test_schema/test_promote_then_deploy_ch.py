#  Project:      dfe-engine
#  File:         tests/integration/test_schema/test_promote_then_deploy_ch.py
#  Purpose:      A promoted field reaches the source's table on the next bare deploy
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Promote a field on a deployed source, deploy with no version, and read the table.

A promote writes a meta-schema version and a source version and changes no table. The bare
deploy that follows applies the new version, so the column has to come out of it. A source
with a derived schema is the case that lost it: the derived selection was written before the
promote, so it cannot name the new column, and the build narrowed the column away. Both
shapes are driven through the API and judged on what ``system.columns`` holds.
"""

from pathlib import Path

import pytest

from dfe_engine.schema.schema_loader import SchemaLoader
from tests.support.engine_stack import deploy_source

pytestmark = [pytest.mark.integration, pytest.mark.own_clickhouse]

API = "/api/v1"
META = "meta/syslog"
DERIVED = "derived/promote/narrow"
JSON_PATH = "probe.value"
COLUMN = "probe_value"


def _columns(client, database: str, table: str) -> set[str]:
    rows = client.query(
        "SELECT name FROM system.columns WHERE database = {db:String} AND table = {tbl:String}",
        parameters={"db": database, "tbl": table},
    ).result_rows
    return {str(row[0]) for row in rows}


def _write_derived_schema(stack) -> list[str]:
    """A derived schema selecting two of the meta schema's columns, and the two it selected."""
    shipped = Path(stack.client.app.state.settings.schemas.schemas_dir) / "meta" / "syslog.yaml"
    names = [column.name for column in SchemaLoader.load_columns(shipped)][:2]
    written = stack.client.post(
        f"{API}/schemas/definitions/{DERIVED}",
        headers=stack.headers,
        json={
            "base": META,
            "base_version": SchemaLoader.load_version_metadata(shipped)["current"],
            "current": "1.0.0",
            "versions": {
                "1.0.0": {
                    "date": "2026-11-24",
                    "summary": "two columns",
                    "select": [{"name": name} for name in names],
                }
            },
        },
    )
    assert written.status_code == 201, written.text
    return names


def _deploy(stack, name: str, *, dry_run: bool = False):
    return stack.client.post(
        f"{API}/sources/{name}/deploy",
        headers=stack.headers,
        params={"dry_run": "true"} if dry_run else {},
    )


@pytest.mark.parametrize("narrowed", [False, True], ids=["whole-meta-schema", "derived-selection"])
def test_a_promoted_field_reaches_the_table_on_the_next_bare_deploy(ch_client, stack, narrowed):
    name = "promonarrow" if narrowed else "promowhole"
    schema = {"meta_schema": META}
    selected: list[str] = []
    if narrowed:
        selected = _write_derived_schema(stack)
        schema["derived_schema"] = DERIVED
    deploy_source(name=name, schema=schema, stack=stack)
    before = _columns(ch_client, stack.database, name)
    if narrowed:
        assert set(selected) <= before
        assert COLUMN not in before

    promoted = stack.client.post(
        f"{API}/schemas/{name}/promote-field",
        headers=stack.headers,
        json={"json_path": JSON_PATH, "column_name": COLUMN, "data_type": "string"},
    )
    assert promoted.status_code == 200, promoted.text
    assert COLUMN not in _columns(ch_client, stack.database, name), "a promote changes no table"
    planned = _deploy(stack, name, dry_run=True)
    deployed = _deploy(stack, name)

    assert deployed.status_code == 200, deployed.text
    assert _columns(ch_client, stack.database, name) == before | {COLUMN}
    assert planned.status_code == 200, planned.text
    assert planned.json()["columns_added"] == [COLUMN]
    assert deployed.json()["columns_added"] == [COLUMN]
    assert _deploy(stack, name, dry_run=True).json()["columns_added"] == []
