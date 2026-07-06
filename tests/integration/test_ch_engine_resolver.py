#  Project:      dfe-engine
#  File:         tests/integration/test_ch_engine_resolver.py
#  Purpose:      Engine resolver renders the right engine per live topology
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The engine resolver renders the right engine for the LIVE topology.

This is the single -> cluster -> cloud regression net (Derek: "we OFTEN find
breakages moving single node -> cluster -> cloud"). It runs on the CANON 3-target
matrix (see tests/integration/conftest.py): a plain and a Replacing MergeTree are
resolved + created FOR REAL on each present target, and the actual
``system.tables.engine`` is asserted against what that topology must yield:

  local single node    -> MergeTree / ReplacingMergeTree
  cluster (multi-node) -> ReplicatedMergeTree / ReplicatedReplacingMergeTree
  ClickHouse Cloud     -> SharedMergeTree / SharedReplacingMergeTree

Where a scale target is present the test also round-trips a row through the table,
proving the resolved engine actually works on that topology (not just parses).
Absent targets skip - CI with only local docker still covers the single-node path.
"""

from __future__ import annotations

import pytest

from dfe_engine.clickhouse.engines import EngineResolver, EngineSpec

# The engine families each matrix target may legitimately produce. A cluster may be
# provisioned as a Replicated database (-> Replicated*) OR CH Cloud-style Shared;
# Cloud auto-substitutes Shared. We assert membership in the target's allowed set.
_ALLOWED_ENGINES: dict[str, set[str]] = {
    "local": {"MergeTree", "ReplacingMergeTree"},
    "cluster": {
        "ReplicatedMergeTree",
        "ReplicatedReplacingMergeTree",
        "SharedMergeTree",
        "SharedReplacingMergeTree",
    },
    "cloud": {
        "SharedMergeTree",
        "SharedReplacingMergeTree",
        "ReplicatedMergeTree",
        "ReplicatedReplacingMergeTree",
    },
}

_EXPECTED_TOPOLOGY = {"local": "single", "cluster": "replicated", "cloud": "replicated"}


@pytest.mark.integration
class TestEngineResolverAcrossTopologies:
    """Resolve + create real tables on every present target; assert the engine."""

    def test_sensed_topology_matches_target(
        self, clickhouse_client, clickhouse_test_database, ch_conn
    ):
        """The resolver senses the right topology class for this live target."""
        resolver = EngineResolver(client=clickhouse_client)
        sensed = resolver.sensed_topology(clickhouse_test_database)
        assert sensed == _EXPECTED_TOPOLOGY[ch_conn["id"]]

    @pytest.mark.parametrize(
        ("variant", "params", "table"),
        [
            ("MergeTree", "", "t_plain"),
            ("ReplacingMergeTree", "ver", "t_repl"),
        ],
    )
    def test_engine_matches_live_topology(
        self,
        clickhouse_client,
        clickhouse_test_database,
        ch_conn,
        variant,
        params,
        table,
    ):
        target_id = ch_conn["id"]
        db = clickhouse_test_database
        resolver = EngineResolver(client=clickhouse_client)
        resolved = resolver.resolve(EngineSpec(variant, params), db)

        clickhouse_client.command(
            f"CREATE TABLE {db}.{table}{resolved.on_cluster} "
            f"(id UInt64, ver UInt64) ENGINE = {resolved.clause} ORDER BY id"
        )
        engine = clickhouse_client.query(
            "SELECT engine FROM system.tables WHERE database = %(db)s AND name = %(t)s",
            parameters={"db": db, "t": table},
        ).result_rows[0][0]
        assert engine in _ALLOWED_ENGINES[target_id], (
            f"{target_id}: {variant} resolved to {resolved.clause!r} but the server "
            f"created engine {engine!r}, not in {_ALLOWED_ENGINES[target_id]}"
        )

        # Scale check: round-trip a row so we know the engine works, not just parses.
        clickhouse_client.insert(table, [[1, 1]], column_names=["id", "ver"], database=db)
        count = clickhouse_client.query(f"SELECT count() FROM {db}.{table}").result_rows[0][0]
        assert count >= 1
