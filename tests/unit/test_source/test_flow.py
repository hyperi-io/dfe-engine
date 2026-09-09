#  Project:      dfe-engine
#  File:         tests/unit/test_source/test_flow.py
#  Purpose:      The source-flow resolver: stages, endpoints, topics, refusals
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What a source resolves to, on each transport, and what it is refused for.

The resolver is the one place the flow's conventions live, so these tests assert
the VALUES it produces - the topic pair, the endpoint, the instance name - rather
than that a resolve happened.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from dfe_engine.appmgmt import catalogue
from dfe_engine.settings import DFESettings
from dfe_engine.source.flow import FlowError, resolve_flow
from dfe_engine.source.models import Source


def _settings(**transport) -> DFESettings:
    """Settings differing from the defaults only in the transport block."""
    return DFESettings(env="dev", transport=transport)


def _source(name: str = "auth", **fields) -> Source:
    data: dict = {"source": name, "match": {"field": "_source", "value": name}, **fields}
    return Source.model_validate(data)


def _fetched(name: str = "okta", **fields) -> Source:
    data: dict = {"source": name, "fetcher": {"source_type": "okta"}, **fields}
    return Source.model_validate(data)


BUS_ONLY_ENGINES = sorted(
    engine
    for engine in catalogue.transform_engines()
    if not catalogue.descriptor(catalogue.transform_service(engine)).carries("direct")
)
"""The transforms still waiting on a Push listener, read off the shipped manifest."""


class TestBusTransport:
    def test_receiver_source_lands_on_its_own_topic(self):
        flow = resolve_flow(_source(), _settings(default="bus"))

        assert flow.transport == "bus"
        assert flow.carrier == "kafka"
        assert flow.origin == "receiver"
        assert flow.input == "_source equals auth"
        assert flow.transform is None
        assert flow.outputs.loader == "auth_land"
        assert flow.outputs.archive is False
        assert flow.table == "auth"

    def test_transform_puts_the_load_topic_between_the_stages(self):
        flow = resolve_flow(
            _source(transform={"engine": "vrl", "variant": "okta_system"}),
            _settings(default="bus"),
        )

        assert flow.transform is not None
        assert flow.transform.app == "dfe-transform-vrl"
        assert flow.transform.instance == "dfe-transform-vrl-auth"
        assert flow.transform.variant == "okta_system"
        assert flow.transform.topics == ("auth_land", "auth_load")
        assert flow.transform.endpoint is None
        # The loader reads whatever the LAST stage wrote.
        assert flow.outputs.loader == "auth_load"

    def test_fetcher_source_names_its_own_instance_as_the_input(self):
        flow = resolve_flow(_fetched(), _settings(default="bus"))

        assert flow.origin == "fetcher"
        assert flow.input == "dfe-fetcher-okta"
        assert flow.outputs.loader == "okta_land"

    def test_archive_is_allowed_on_the_bus(self):
        flow = resolve_flow(_source(archive=True), _settings(default="bus"))

        assert flow.outputs.archive is True


class TestDirectTransport:
    def test_receiver_source_pushes_straight_at_the_loader(self):
        flow = resolve_flow(_source(transport="direct"), _settings(default="direct"))

        assert flow.transport == "direct"
        assert flow.carrier == "grpc"
        assert flow.transform is None
        assert flow.outputs.loader == "http://dfe-loader:6000"

    def test_transform_answers_on_its_own_instance_service(self):
        flow = resolve_flow(
            _source(transport="direct", transform={"engine": "vrl"}),
            _settings(default="direct"),
        )

        assert flow.transform is not None
        assert flow.transform.instance == "dfe-transform-vrl-auth"
        assert flow.transform.endpoint == "http://dfe-transform-vrl-auth:6000"
        assert flow.transform.topics is None
        assert flow.outputs.loader == "http://dfe-loader:6000"

    def test_fetcher_source_keeps_its_instance_input(self):
        flow = resolve_flow(_fetched(transport="direct"), _settings(default="direct"))

        assert flow.origin == "fetcher"
        assert flow.input == "dfe-fetcher-okta"
        assert flow.outputs.loader == "http://dfe-loader:6000"


class TestMesh:
    """Where a deployment balances its pools behind listeners, every address moves."""

    NAMESPACE = "envoy-gateway-system"

    def _mesh(self) -> DFESettings:
        return _settings(default="direct", mesh_enabled=True, mesh_namespace=self.NAMESPACE)

    def test_the_loader_is_addressed_at_its_listener_alias(self):
        flow = resolve_flow(_source(), self._mesh())

        assert flow.outputs.loader == (
            f"http://dfe-loader-mesh.{self.NAMESPACE}.svc.cluster.local:6000"
        )

    def test_a_transform_pool_is_addressed_at_its_own_alias(self):
        flow = resolve_flow(_source(transform={"engine": "vrl"}), self._mesh())

        assert flow.transform is not None
        assert flow.transform.endpoint == (
            f"http://dfe-transform-vrl-auth-mesh.{self.NAMESPACE}.svc.cluster.local:6000"
        )
        assert flow.outputs.loader == (
            f"http://dfe-loader-mesh.{self.NAMESPACE}.svc.cluster.local:6000"
        )

    def test_a_namespace_alone_changes_no_address(self):
        # The namespace reaches the engine on every profile; the switch is what
        # decides whether a sender uses it.
        flow = resolve_flow(
            _source(transform={"engine": "vrl"}),
            _settings(default="direct", mesh_namespace=self.NAMESPACE),
        )

        assert flow.transform is not None
        assert flow.transform.endpoint == "http://dfe-transform-vrl-auth:6000"
        assert flow.outputs.loader == "http://dfe-loader:6000"

    def test_the_bus_is_unaffected(self):
        flow = resolve_flow(
            _source(transform={"engine": "vrl"}),
            _settings(default="bus", mesh_enabled=True, mesh_namespace=self.NAMESPACE),
        )

        assert flow.transform is not None
        assert flow.transform.topics == ("auth_land", "auth_load")
        assert flow.outputs.loader == "auth_load"


class TestDeploymentDefault:
    def test_an_unset_transport_takes_the_deployment_default(self):
        assert resolve_flow(_source(), _settings(default="bus")).transport == "bus"
        assert (
            resolve_flow(_source(), _settings(default="direct", bus_present=False)).transport
            == "direct"
        )

    def test_a_second_bus_provider_needs_no_source_change(self):
        flow = resolve_flow(_source(), _settings(default="bus", bus_provider="pulsar"))

        assert flow.transport == "bus"
        assert flow.carrier == "pulsar"


class TestRefusals:
    def test_bus_source_on_a_brokerless_deployment(self):
        with pytest.raises(FlowError, match="offers direct"):
            resolve_flow(_source(transport="bus"), _settings(default="direct", bus_present=False))

    def test_archive_on_direct(self):
        with pytest.raises(FlowError, match="archive needs the bus transport"):
            resolve_flow(_source(transport="direct", archive=True), _settings(default="direct"))

    def test_transform_that_does_not_carry_the_transport(self):
        with pytest.raises(FlowError, match="dfe-transform-elastic carries only bus"):
            resolve_flow(
                _source(transport="direct", transform={"engine": "elastic"}),
                _settings(default="direct"),
            )

    @pytest.mark.parametrize("engine", BUS_ONLY_ENGINES)
    def test_a_transform_without_a_listener_refuses_direct(self, engine):
        # Shipping the listener and declaring it is the whole change; nothing
        # here names an app.
        with pytest.raises(FlowError, match="carries only bus"):
            resolve_flow(
                _source(transport="direct", transform={"engine": engine}),
                _settings(default="direct"),
            )

    def test_transform_app_missing_from_the_catalogue(self):
        stripped = {
            name: app
            for name, app in catalogue.APP_CATALOGUE.items()
            if name != "dfe-transform-vrl"
        }
        with pytest.raises(FlowError, match="no such app is catalogued"):
            resolve_flow(_source(transform={"engine": "vrl"}), _settings(default="bus"), stripped)

    def test_a_bus_only_loader_refuses_a_direct_source(self):
        # Every stage is checked, not just the optional one.
        bus_only = dict(catalogue.APP_CATALOGUE)
        bus_only["dfe-loader"] = replace(bus_only["dfe-loader"], transports=frozenset({"bus"}))

        with pytest.raises(FlowError, match="dfe-loader carries only bus"):
            resolve_flow(_source(transport="direct"), _settings(default="direct"), bus_only)

    def test_a_transform_on_direct_needs_a_match_the_receiver_can_route_on(self):
        # The receiver picks a destination on field AND value, so an `exists`
        # match would send the source to the loader untransformed.
        with pytest.raises(FlowError, match="tests no value"):
            resolve_flow(
                Source.model_validate(
                    {
                        "source": "auth",
                        "match": {"field": "_json.app", "operator": "exists"},
                        "transport": "direct",
                        "transform": {"engine": "vrl"},
                    }
                ),
                _settings(default="direct"),
            )

    def test_the_same_match_is_fine_without_a_transform(self):
        flow = resolve_flow(
            Source.model_validate(
                {
                    "source": "auth",
                    "match": {"field": "_json.app", "operator": "exists"},
                    "transport": "direct",
                }
            ),
            _settings(default="direct"),
        )

        assert flow.outputs.loader == "http://dfe-loader:6000"

    def test_a_bus_only_fetcher_refuses_a_direct_source(self):
        bus_only = dict(catalogue.APP_CATALOGUE)
        bus_only["dfe-fetcher"] = replace(bus_only["dfe-fetcher"], transports=frozenset({"bus"}))

        with pytest.raises(FlowError, match="dfe-fetcher carries only bus"):
            resolve_flow(_fetched(transport="direct"), _settings(default="direct"), bus_only)
