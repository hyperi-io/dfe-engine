#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_internal_tables.py
#  Purpose:      Hunt coordination tables collapse retries, and the heartbeat expires
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The engine's own ClickHouse tables, as the applier actually resolves them.

Only ``hunt_run`` and ``hunt_runner_heartbeat`` are declared in this repo; the rest
of the hunt coordination set is loaded from the installed dfe-schemas package, which
releases on its own cycle. The insert path retries, so every one of them has to be a
ReplacingMergeTree or a re-inserted row is a duplicate rather than a collapse.
"""

from __future__ import annotations

import pytest

from dfe_engine.schema.internal_tables import (
    HUNT_COORDINATION_REFS,
    hunt_coordination_specs,
    hunt_runner_heartbeat_spec,
)
from dfe_engine.schema.schema_ddl import DDLGenerator
from dfe_engine.schema.table_loader import load_table_spec
from dfe_engine.source.type_registry import TypeRegistry


@pytest.mark.parametrize("ref", HUNT_COORDINATION_REFS)
def test_a_packaged_coordination_table_is_a_replacing_merge_tree(ref):
    spec = load_table_spec(ref, "dfe")
    assert spec.config.engine.startswith("ReplacingMergeTree"), (
        f"{ref} is {spec.config.engine}: a retried insert would duplicate the row"
    )


def test_every_coordination_table_collapses_a_retried_insert():
    engines = {spec.name: spec.config.engine for spec in hunt_coordination_specs("dfe")}
    assert engines
    assert all(engine.startswith("ReplacingMergeTree") for engine in engines.values()), engines


def test_the_heartbeat_table_expires_its_rows():
    # runner_id is the pod hostname, so a rollout mints keys nothing counts again.
    spec = hunt_runner_heartbeat_spec("dfe")
    assert spec.config.ttl_days == 7
    assert spec.config.ttl_columns == ["updated"]

    generator = DDLGenerator(TypeRegistry.default())
    ddl = generator.generate_create_table(spec.name, spec.columns, spec.config)
    assert "TTL updated + INTERVAL 7 DAY DELETE" in ddl
