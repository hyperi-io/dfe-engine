#  Project:      dfe-engine
#  File:         tests/integration/test_schema/test_refused_drift_keeps_columns.py
#  Purpose:      A refused change on a core table never holds back a column it gains
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A dfe-schemas release that adds a column lands, whatever else the table refuses.

The shape this guards: a deployment changes ``DFE_CLICKHOUSE_DEFAULT_TTL_DAYS`` and
takes a dfe-schemas release that adds a column to the landing table, in the same
boot. The TTL change is refused as drift, which is right. The column must still be
added, or every writer that sends it fails against a table that never gets it.

Against a real server, over a copy of the shipped schema tree, so the column arrives
the way a release delivers it: in the ``timeseries`` profile ``data.main`` takes its
columns from.
"""

import re
import shutil
import uuid
from pathlib import Path

import dfe_schemas
import pytest

from dfe_engine.schema.phase import apply_plan
from dfe_engine.schema.plan import build_plan
from dfe_engine.settings import ClickHouseSettings, DFESettings

pytestmark = pytest.mark.integration

NEW_COLUMN = "_release_added"
_TTL_DAYS_RE = re.compile(r"toIntervalDay\((\d+)\)")


@pytest.fixture
def schemas_root(monkeypatch, tmp_path) -> Path:
    """A copy of the shipped schema tree the plan renders from."""
    root = tmp_path / "schemas"
    shutil.copytree(Path(dfe_schemas.__file__).parent / "data", root)
    monkeypatch.setenv("DFE_SCHEMAS_DIR", str(root))
    return root


@pytest.fixture
def database(ch_client):
    """A database name that does not exist yet, dropped afterwards."""
    name = f"dfe_drift_{uuid.uuid4().hex[:8]}"
    yield name
    try:
        ch_client.command(f"DROP DATABASE IF EXISTS `{name}` SYNC")
    except Exception:
        pass


def _settings(database: str, days: int) -> DFESettings:
    return DFESettings(
        env="dev",
        clickhouse=ClickHouseSettings(data_database=database, default_ttl_days=days),
    )


def _release_a_column(root: Path) -> None:
    """Add a column to the current timeseries profile, as a dfe-schemas release would.

    The current version is read from the file, because ``data.main`` takes its
    columns from whichever version the profile names current.
    """
    profile = root / "common-header" / "timeseries.yaml"
    text = profile.read_text(encoding="utf-8")
    current = re.search(r'^current: "([^"]+)"', text, re.MULTILINE)
    assert current, "the timeseries profile names no current version"
    head, version, tail = text.partition(f'  "{current.group(1)}":')
    before, columns, rest = tail.partition("    columns:\n")
    assert version, f"no version block for the current version {current.group(1)}"
    assert columns, f"no columns under the current version {current.group(1)}"
    column = f"      - name: {NEW_COLUMN}\n        type: string\n        _field_type: base\n\n"
    profile.write_text(head + version + before + columns + column + rest, encoding="utf-8")


def _live_columns(ch_client, database: str, table: str) -> set[str]:
    rows = ch_client.query(
        "SELECT name FROM system.columns WHERE database = {db:String} AND table = {t:String}",
        parameters={"db": database, "t": table},
    ).result_rows
    return {str(row[0]) for row in rows}


def _live_ttl_days(ch_client, database: str, table: str) -> int | None:
    rows = ch_client.query(
        "SELECT engine_full FROM system.tables WHERE database = {db:String} AND name = {t:String}",
        parameters={"db": database, "t": table},
    ).result_rows
    match = _TTL_DAYS_RE.search(str(rows[0][0]).partition(" TTL ")[2])
    return int(match.group(1)) if match else None


def test_a_refused_ttl_change_still_adds_the_released_column(ch_client, database, schemas_root):
    apply_plan(ch_client, build_plan(settings=_settings(database, 90), client=ch_client))
    _release_a_column(schemas_root)

    report = apply_plan(ch_client, build_plan(settings=_settings(database, 30), client=ch_client))

    main = next(outcome for outcome in report.outcomes if outcome.id == "data.main")
    assert main.action == "refused"
    assert main.drift == ("TTL is 90 days, the schema declares 30",)
    assert main.columns_added == (NEW_COLUMN,)
    assert f"added 1 column(s) [{NEW_COLUMN}]" in main.describe()
    assert NEW_COLUMN in _live_columns(ch_client, database, "main")
    assert _live_ttl_days(ch_client, database, "main") == 90


def test_the_next_pass_still_refuses_the_ttl_and_adds_nothing_twice(
    ch_client, database, schemas_root
):
    apply_plan(ch_client, build_plan(settings=_settings(database, 90), client=ch_client))
    _release_a_column(schemas_root)
    changed = build_plan(settings=_settings(database, 30), client=ch_client)
    apply_plan(ch_client, changed)

    again = apply_plan(ch_client, changed)

    main = next(outcome for outcome in again.outcomes if outcome.id == "data.main")
    assert main.action == "refused"
    assert main.columns_added == ()
    assert not [s for s in again.statements if "ADD COLUMN" in s and "`main`" in s]
