#  Project:      dfe-engine
#  File:         tests/integration/test_overview_throughput.py
#  Purpose:      Generated throughput view spans _timestamp_load tables (real CH)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Live proof: the engine-generated overall-throughput view counts rows per bucket
across EVERY _timestamp_load-bearing data table, and does NOT error on the hunt
coordination tables that share the data database but lack that column.

This is the exact trap that ruled out a static merge('.*') view (which errors,
code 10, when a merged table lacks the selected column) - so the view is generated
from the sensed table set. Runs on the CANON matrix via ch_conn.
"""

from __future__ import annotations

import pytest

from dfe_engine.query.ddl import DDLManager

pytestmark = pytest.mark.integration


def test_generated_throughput_view_spans_data_tables_only(ch_conn, clickhouse_test_database):
    import clickhouse_connect

    db = clickhouse_test_database
    conn = {k: v for k, v in ch_conn.items() if k != "id"}
    client = clickhouse_connect.get_client(database=db, **conn)

    # Two DATA tables (landing + a per-source table, both with _timestamp_load) and
    # a COORDINATION table (hunt_lease) that lacks it - exactly the data-db mix.
    for tbl in ("default", "apache"):
        client.command(
            f"CREATE TABLE {db}.{tbl} (_timestamp_load DateTime64(3), _raw String) "
            "ENGINE = MergeTree ORDER BY _timestamp_load"
        )
    client.command(
        f"CREATE TABLE {db}.hunt_lease (hunt_id String, until DateTime) "
        "ENGINE = MergeTree ORDER BY hunt_id"
    )
    client.command(f"INSERT INTO {db}.default VALUES (now(), 'a'), (now(), 'bb')")
    client.command(f"INSERT INTO {db}.apache VALUES (now(), 'ccc')")
    client.command(f"INSERT INTO {db}.hunt_lease VALUES ('h1', now())")

    mgr = DDLManager(client=client, database=db)

    # Sensing finds ONLY the two data tables (coordination table excluded).
    assert set(mgr._timestamp_load_tables()) == {"default", "apache"}

    # Create the generated view directly ({db} substituted; skip the reader GRANT,
    # which needs access_management the throwaway CH may not grant).
    tables = mgr._timestamp_load_tables()
    client.command(mgr._render_throughput_sql(tables).replace("{db}", db))

    # Execute it - must count across default+apache (3 rows) and NOT error on
    # hunt_lease. If the generated merge() had matched hunt_lease, this would raise.
    result = client.query(
        f"SELECT sum(rows) AS total FROM {db}.dfe_v_overview_ingest_rows_bytes("
        "bucket_minutes = 60, "
        "time_from = now() - toIntervalHour(1), "
        "time_to = now() + toIntervalHour(1))"
    )
    assert result.result_rows[0][0] == 3
