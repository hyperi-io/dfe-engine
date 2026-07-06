#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_migrations.py
#  Purpose:      Migration runner - applies pending, records, idempotent, skips done
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Migration runner logic against a fake wrapper (no live CH): apply once, dedup."""

from __future__ import annotations

from dfe_engine.clickhouse.engines import ResolvedEngine
from dfe_engine.clickhouse.migrations import run_migrations


class _FakeWrapper:
    """Captures DDL + simulates the schema_migrations tracking table."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.inserts: list[tuple] = []
        self._applied: list[str] = []

    def with_profile(self, _profile):
        return self  # the runner rebinds to MIGRATE; the fake is profile-agnostic

    def resolve_engine(self, spec, _database):
        return ResolvedEngine(
            clause=f"{spec.variant}({spec.params})",
            on_cluster="",
            topology="single",
            origin="test",
        )

    def command(self, statement: str):
        self.commands.append(statement)

    def query_rows(self, _sql, *_a, **_k):
        return ["id"], [(mid,) for mid in self._applied]

    def insert(self, table, data, *, column_names, database=None):
        self.inserts.append((table, data, database))
        self._applied.extend(row[0] for row in data)  # simulate persistence


def test_applies_pending_and_records_tracking():
    w = _FakeWrapper()
    newly = run_migrations(w)
    assert newly == ["0001"]
    # tracking DB + table created, then the migration DDL applied.
    assert any("schema_migrations" in c for c in w.commands)
    assert any("query_log_archive" in c for c in w.commands)
    # the applied id was recorded in the tracking table.
    assert w.inserts
    assert w.inserts[0][0] == "schema_migrations"
    assert w.inserts[0][1] == [("0001", "query_log_archive")]


def test_second_run_is_idempotent_noop():
    w = _FakeWrapper()
    run_migrations(w)
    inserts_after_first = len(w.inserts)
    newly = run_migrations(w)  # everything already applied
    assert newly == []
    assert len(w.inserts) == inserts_after_first  # no new tracking rows


def test_flushes_logs_before_query_log_migration():
    # The runner must flush system.query_log (lazy) before a MV that reads it.
    w = _FakeWrapper()
    run_migrations(w)
    assert "SYSTEM FLUSH LOGS" in w.commands
