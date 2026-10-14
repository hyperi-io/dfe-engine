#  Project:      dfe-engine
#  File:         tests/unit/test_backing/test_observed.py
#  Purpose:      Observed sizing reads: a failed read is unknown, never an error
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""An observed size is shown, never acted on, so every failure reads as unknown.

The broker count used to be readable only through the replication clamp, which
turns a failure into 1. A card showing "1 broker" for an unreachable cluster
states a size nobody measured, so the read now says None and the clamp keeps its 1.

ClickHouse's successful read is pinned against a real server in
tests/integration/test_backing_observed_ch.py; the down-server path is here.
"""

import pytest

from dfe_engine.backing import observed
from dfe_engine.kafka.topics import TopicAdmin, broker_count, read_broker_count
from dfe_engine.settings import DFESettings
from tests.support.down_clickhouse import down_clickhouse


class _ReportingAdmin(TopicAdmin):
    """The admin seam the topic module injects, answering one node count or failing.

    It builds no AdminClient: the conftest guard refuses one in a unit test.
    """

    def __init__(self, nodes: int | None = None, error: Exception | None = None) -> None:
        self._nodes = nodes
        self._error = error

    def node_count(self, *, timeout: float = 10.0) -> int:
        if self._error is not None:
            raise self._error
        assert self._nodes is not None
        return self._nodes


class TestBrokerCount:
    def test_the_cluster_metadata_count_is_returned(self):
        assert read_broker_count(admin=_ReportingAdmin(nodes=3)) == 3

    @pytest.mark.parametrize("nodes", [0, -1])
    def test_a_bus_reporting_no_brokers_is_unknown(self, nodes):
        assert read_broker_count(admin=_ReportingAdmin(nodes=nodes)) is None

    def test_an_unreachable_bus_is_unknown(self):
        assert read_broker_count(admin=_ReportingAdmin(error=OSError("no route"))) is None

    def test_a_refused_admin_client_is_unknown(self):
        """The conftest guard refuses the real AdminClient, as a broken client would."""
        assert read_broker_count(settings=DFESettings(env="test")) is None

    def test_the_replication_clamp_still_reads_one_where_the_count_is_unknown(self):
        assert broker_count(admin=_ReportingAdmin(error=OSError("no route"))) == 1
        assert broker_count(admin=_ReportingAdmin(nodes=5)) == 5


class TestKafkaBrokers:
    def test_a_deployment_with_no_bus_never_asks(self, monkeypatch):
        settings = DFESettings(env="test")
        settings.transport.bus_present = False
        settings.transport.default = "direct"

        def refuse(**_kwargs):
            raise AssertionError("the broker was asked on a deployment with no bus")

        monkeypatch.setattr("dfe_engine.kafka.topics.build_admin", refuse)

        assert observed.kafka_brokers(settings) is None

    def test_a_bus_that_cannot_be_asked_is_unknown(self):
        assert observed.kafka_brokers(DFESettings(env="test")) is None


class TestClickHouseTopology:
    def test_a_down_server_is_unknown_and_was_dialled(self):
        settings = DFESettings(env="test")
        with down_clickhouse(settings) as (listener, client):
            found = observed.clickhouse_topology(lambda: client)

            assert found == observed.ClickHouseTopology()
            assert listener.connections >= 1

    def test_a_client_that_cannot_be_built_is_unknown(self):
        def unbuildable():
            raise RuntimeError("no settings to build a client from")

        assert observed.clickhouse_topology(unbuildable) == observed.ClickHouseTopology()


class TestCloudService:
    def test_an_unconfigured_control_plane_is_never_asked(self):
        from dfe_engine.settings import ClickHouseCloudSettings

        assert observed.cloud_service(ClickHouseCloudSettings(), timeout=0.1) is None
