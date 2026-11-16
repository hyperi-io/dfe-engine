#  Project:      dfe-engine
#  File:         tests/integration/test_schema/test_manifest_view_recreate_ch.py
#  Purpose:      A deployment whose archive view changed comes up on the new SELECT with its rows kept
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The query_log_archive view follows its SELECT across an upgrade, on real ClickHouse.

The first pass creates the ledger, the archive table and its view and records
them. A row lands in the table. The second pass renders the view with a changed
SELECT, as a later dfe-schemas does: the view is dropped and created again, the
table keeps its row, and the ledger holds the new checksum.
"""

import dataclasses
from importlib.metadata import version

from dfe_schemas.clickhouse import PlatformProbe

from dfe_engine.clickhouse.query_log_archive import ARCHIVE_ID
from dfe_engine.schema.applier import ManifestApplier
from dfe_engine.schema.ledger import MigrationLedger
from dfe_engine.schema.plan import LEDGER_ID, object_names, render_one


def _applier(ch, db: str, platform) -> ManifestApplier:
    applier = ManifestApplier(ch, platform, schemas_version=version("dfe-schemas"))
    ledger = MigrationLedger(
        ch, database=db, table=object_names(LEDGER_ID, data_database=db)[LEDGER_ID]
    )
    applier.attach_ledger(ledger)
    return applier


def test_a_changed_archive_select_is_applied_and_the_rows_stay(ch_client, dfe_db):
    platform = PlatformProbe(ch_client).platform
    ch_client.command("SYSTEM FLUSH LOGS")
    view_id = f"{ARCHIVE_ID}_mv"
    rendered = {
        object_id: render_one(object_id, data_database=dfe_db, platform=platform)
        for object_id in (LEDGER_ID, ARCHIVE_ID, view_id)
    }
    # The ledger's own table comes first, as at boot, so the applier can read it.
    for statement in rendered[LEDGER_ID].statements:
        ch_client.command(statement)
    first = _applier(ch_client, dfe_db, platform)
    assert [first.apply(rendered[o]).action for o in (ARCHIVE_ID, view_id)] == [
        "created",
        "created",
    ]
    first.flush()

    archive = f"`{dfe_db}`.`{rendered[ARCHIVE_ID].name}`"
    ch_client.command(
        f"INSERT INTO {archive} (event_time, query_id, query_duration_ms, log_comment) "
        "VALUES (now(), 'kept', 7, '{}')"
    )

    old_view = rendered[view_id]
    changed = old_view.statements[0].replace(
        "isValidJSON(log_comment)", "isValidJSON(log_comment) AND query_duration_ms >= 0"
    )
    assert changed != old_view.statements[0]
    new_view = dataclasses.replace(old_view, statements=(changed,), checksum="changed")

    second = _applier(ch_client, dfe_db, platform)
    assert second.apply(new_view).action == "altered"
    second.flush()

    rows = ch_client.query(f"SELECT query_id FROM {archive}").result_rows
    assert [r[0] for r in rows] == ["kept"]
    live = ch_client.query(
        "SELECT create_table_query FROM system.tables WHERE database = {db:String} AND name = {n:String}",
        parameters={"db": dfe_db, "n": old_view.name},
    ).result_rows[0][0]
    assert "query_duration_ms >= 0" in live
    assert _applier(ch_client, dfe_db, platform).apply(new_view).action == "unchanged"
