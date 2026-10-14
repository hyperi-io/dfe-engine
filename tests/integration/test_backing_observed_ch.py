#  Project:      dfe-engine
#  File:         tests/integration/test_backing_observed_ch.py
#  Purpose:      Observed ClickHouse sizing, read off a real server through the engine's client
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A managed ClickHouse card shows what the server itself reports.

Read through the engine's own client wrapper, because the console read goes
through that wrapper's resilience and server-side parameter binding, and an array
parameter that one driver binds and another refuses would blank the card silently.
"""

from dfe_engine.backing import observed


def _hosts(ch_client, cluster: str) -> int:
    return ch_client.query(
        "SELECT count() FROM system.clusters WHERE cluster = {c:String}", parameters={"c": cluster}
    ).result_rows[0][0]


def _measured_cluster(ch_client) -> str | None:
    macro = ch_client.query("SELECT substitution FROM system.macros WHERE macro = 'cluster'")
    if macro.result_rows and macro.result_rows[0][0]:
        return macro.result_rows[0][0]
    return observed.DEFAULT_CLUSTER if _hosts(ch_client, observed.DEFAULT_CLUSTER) else None


def test_the_server_reports_the_memory_it_may_use(manager_client):
    found = observed.clickhouse_topology(lambda: manager_client)

    assert found.memory_bytes is not None
    assert found.memory_bytes > 0


def test_replicas_and_shards_account_for_every_host_in_the_cluster(manager_client, ch_client):
    """A uniform cluster's hosts are its shards times its replicas, counted another way."""
    found = observed.clickhouse_topology(lambda: manager_client)
    cluster = _measured_cluster(ch_client)

    if cluster is None:
        assert (found.replicas, found.shards) == (None, None)
        return
    assert found.replicas is not None
    assert found.shards is not None
    assert found.replicas >= 1
    assert found.shards >= 1
    assert found.replicas * found.shards == _hosts(ch_client, cluster)
