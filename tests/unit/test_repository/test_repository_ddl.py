#  Project:      dfe-engine
#  File:         test_repository_ddl.py
#  Purpose:      Guards for the repository table spec (dedup invariants + topology)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Guards for ``dfe_internal.repository``, rendered from its spec.

The spec in :mod:`dfe_engine.schema.internal_tables` is the source of truth --
there is no bundled ``.sql`` and no canonical copy in dfe-schemas to drift from,
because the engine clause is not knowable until the target server is sensed.

What is asserted here are the invariants a rendering must never break: the dedup
key, the absence of a partition, and the topology forms the resolver produces.
"""

from __future__ import annotations

from dfe_engine.schema.engine_resolver import EngineResolver
from dfe_engine.schema.internal_tables import repository_spec
from dfe_engine.schema.schema_ddl import DDLGenerator
from dfe_engine.source.type_registry import TypeRegistry

_DB = "dfe_internal"


def _render(resolver: EngineResolver | None = None) -> str:
    spec = repository_spec(_DB)
    generator = DDLGenerator(TypeRegistry.default(), resolver=resolver)
    return generator.generate_create_table(
        table_name=spec.name, columns=spec.columns, config=spec.config
    )


def test_no_org_id_column() -> None:
    """No _org_id by design - keeps ChRbacReconciler _org_id discovery away."""
    assert not any(col.name == "_org_id" for col in repository_spec(_DB).columns)


def test_no_partition_by() -> None:
    """No PARTITION BY - ReplacingMergeTree dedup must stay within one part tree."""
    assert "PARTITION BY" not in _render().upper()


def test_no_projection() -> None:
    """No projection - the data-table default would add one nothing reads."""
    assert "PROJECTION" not in _render().upper()


def test_engine_and_key() -> None:
    """Dedup invariants: ReplacingMergeTree(updated_at, is_deleted) + full scope key."""
    ddl = _render()
    assert "ENGINE = ReplacingMergeTree(updated_at, is_deleted)" in ddl
    assert "ORDER BY (`scope`, `scope_id`, `namespace`, `key`)" in ddl


def test_engine_follows_topology() -> None:
    """A replicated topology keeps the version columns on the Replicated variant."""
    ddl = _render(EngineResolver(override="replicated"))
    assert "ENGINE = ReplicatedReplacingMergeTree(updated_at, is_deleted)" in ddl


def test_targets_the_internal_database() -> None:
    """The table is qualified against whatever database the spec was built for."""
    assert f"CREATE TABLE IF NOT EXISTS `{_DB}`.`repository`" in _render()
