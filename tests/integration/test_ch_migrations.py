#  Project:      dfe-engine
#  File:         tests/integration/test_ch_migrations.py
#  Purpose:      Migration runner on real CH - applies, tracks, idempotent re-run
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Live proof the migration runner applies + tracks + is idempotent.

Runs on the LOCAL throwaway target only: the runner writes the FIXED engine
databases (``dfe_meta.schema_migrations`` / ``dfe_audit.query_log_archive``), not
an isolated test db, so it is only safe against a disposable container. The
DDL-per-topology correctness (single / cluster / cloud) is covered by the
resolver + query_log_archive matrix tests.
"""

from __future__ import annotations

import pytest

from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.clickhouse.migrations import run_migrations

pytestmark = pytest.mark.integration


def _wrapper(ch_conn: dict):
    cfg = {
        "ch_host": ch_conn["host"],
        "ch_port": ch_conn["port"],
        "ch_username": ch_conn.get("username", "default"),
        "ch_password": ch_conn.get("password", ""),
        "ch_secure": ch_conn.get("secure", False),
        "ch_database": "default",
    }
    return ClickHouseManager.get_instance(cfg).get_clickhouse_client()


def test_migration_runner_applies_and_is_idempotent(ch_conn):
    if ch_conn["id"] != "local":
        pytest.skip("runner writes fixed dfe_meta/dfe_audit dbs - local throwaway only")

    ClickHouseManager.reset_instance()
    wrapper = _wrapper(ch_conn)
    try:
        # First run applies 0001 (query_log_archive) and records it.
        first = run_migrations(wrapper)
        assert "0001" in first

        _, tracked = wrapper.query_rows("SELECT id FROM dfe_meta.schema_migrations FINAL")
        assert "0001" in {row[0] for row in tracked}

        # The migration really created the archive table + MV.
        exists = wrapper.command("EXISTS TABLE dfe_audit.query_log_archive")
        assert str(exists).strip() in ("1", "True")

        # Re-run is a pure no-op (nothing pending).
        second = run_migrations(wrapper)
        assert second == []
    finally:
        # Disposable container, but tidy the fixed dbs so a --keep reuse is clean.
        for db in ("dfe_meta", "dfe_audit"):
            try:
                wrapper.command(f"DROP DATABASE IF EXISTS {db}")
            except Exception:
                pass
        ClickHouseManager.reset_instance()
