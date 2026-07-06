#  Project:      dfe-engine
#  File:         clickhouse/migrations.py
#  Purpose:      Topology-aware CH migrations - the bootstrap-apply-that-senses runner
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Topology-aware ClickHouse migrations - apply the engine's DDL, sensing the shape.

Numbered migrations applied ONCE at startup. The runner SENSES the live topology
so every ``CREATE`` gets the right engine + ON CLUSTER form (single ``MergeTree`` /
cluster ``ReplicatedMergeTree`` ON CLUSTER / Cloud auto-``Shared``) - the operator
can never mis-set it, and Cloud is auto-handled. This is decision A of the plan
(a bootstrap step that CONNECTS and applies, so the initial tables are self-sensed
everywhere, while Argo still reconciles the committed artifacts for drift).

Safe to run every startup, and safe for two engine pods to race it:
- every statement is ``IF [NOT] EXISTS`` (idempotent - re-apply no-ops), one
  statement per step (ClickHouse has no transactional DDL);
- applied migrations are recorded in ``dfe_meta.schema_migrations`` (a
  ReplacingMergeTree keyed by id, so a double-insert from a race dedups), and a
  migration whose id is already recorded is skipped.

Pattern from Snuba / PostHog migration runners, fed by our sense-first cascade.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from scalo.logger import logger

from .engines import EngineSpec
from .names import DFE_AUDIT, DFE_META
from .profiles import Profile

_TRACKING = "schema_migrations"


@dataclass(frozen=True, slots=True)
class Migration:
    """One numbered migration: an id, a name, and a DDL renderer.

    ``render`` is handed the runner so it can SENSE the engine for its target
    database (``runner.resolve(spec, db)``) and return the ordered DDL statements -
    every one ``IF [NOT] EXISTS``.
    """

    id: str  # zero-padded, ordered - e.g. "0001"
    name: str
    render: Callable[[MigrationRunner], list[str]]


def _query_log_archive(runner: MigrationRunner) -> list[str]:
    """Migration 0001 - the query_log_archive table + MV (cost leaderboard source)."""
    from .query_log_archive import render_ddl

    engine = runner.resolve(EngineSpec("MergeTree"), DFE_AUDIT)
    return render_ddl(engine)


# The ordered migration registry. Append new migrations; never renumber an applied
# one (the id is the applied-marker). DATA-table DDL (dfe.default / profiles /
# detection_checkpoint / hunt_results) folds in here as it moves off the static
# Argo-.sql path onto bootstrap-apply (a follow-up - see the handover).
MIGRATIONS: list[Migration] = [
    Migration("0001", "query_log_archive", _query_log_archive),
]


class MigrationRunner:
    """Applies pending migrations through a MIGRATE-profile client, sensing engines."""

    def __init__(self, wrapper) -> None:
        # MIGRATE profile: server-side alter/mutations sync + the long ON CLUSTER
        # distributed-DDL window, so every replica acks before a step returns.
        self._w = wrapper.with_profile(Profile.MIGRATE)

    def resolve(self, spec: EngineSpec, database: str):
        """Sense + resolve the engine for ``database`` (delegates to the wrapper)."""
        return self._w.resolve_engine(spec, database)

    def _ensure_tracking(self) -> None:
        engine = self.resolve(EngineSpec("ReplacingMergeTree", "applied_at"), DFE_META)
        self._w.command(f"CREATE DATABASE IF NOT EXISTS {DFE_META}{engine.on_cluster}")
        self._w.command(
            f"CREATE TABLE IF NOT EXISTS {DFE_META}.{_TRACKING}{engine.on_cluster} "
            "(id String, name String, applied_at DateTime DEFAULT now()) "
            f"ENGINE = {engine.clause} ORDER BY id"
        )

    def _applied(self) -> set[str]:
        _, rows = self._w.query_rows(f"SELECT DISTINCT id FROM {DFE_META}.{_TRACKING} FINAL")
        return {row[0] for row in rows}

    def run(self) -> list[str]:
        """Apply every not-yet-applied migration; return the ids applied this run.

        Flushes ``system.query_log`` once up front so a migration whose MV reads it
        (query_log_archive) finds the lazily-created source table present.
        """
        self._ensure_tracking()
        self._w.command("SYSTEM FLUSH LOGS")
        applied = self._applied()
        newly: list[str] = []
        for migration in MIGRATIONS:
            if migration.id in applied:
                continue
            for stmt in migration.render(self):
                self._w.command(stmt)
            self._w.insert(
                _TRACKING,
                [(migration.id, migration.name)],
                column_names=["id", "name"],
                database=DFE_META,
            )
            newly.append(migration.id)
            logger.info("ClickHouse migration applied", id=migration.id, name=migration.name)
        if newly:
            logger.info("ClickHouse migrations complete", applied=len(newly))
        return newly


def run_migrations(wrapper) -> list[str]:
    """Apply pending CH migrations via ``wrapper`` (the bootstrap entry point)."""
    return MigrationRunner(wrapper).run()
