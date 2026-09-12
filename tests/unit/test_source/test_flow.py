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


def _settings(
    *, profile: str = "", target: str = "unknown", app_config_dir: str = "", **transport
) -> DFESettings:
    """Settings differing from the defaults only in the transport block and the deployment."""
    return DFESettings(
        env="dev",
        transport=transport,
        deployment={"profile": profile, "target": target, "app_config_dir": app_config_dir},
    )


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

    def test_archive_is_allowed_on_direct_since_the_archiver_declares_it(self):
        # The sender fans the record out to the archiver's Push listener beside
        # the loader, so a brokerless deployment can still keep the raw record.
        flow = resolve_flow(_source(transport="direct", archive=True), _settings(default="direct"))

        assert flow.outputs.archive is True


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

    def test_archive_on_a_transport_the_archiver_does_not_carry(self):
        # The refusal follows the manifest, so an archiver that gives up its
        # listener refuses direct again with no engine change.
        bus_only = dict(catalogue.APP_CATALOGUE)
        bus_only["dfe-archiver"] = replace(
            catalogue.descriptor("dfe-archiver"), transports=frozenset({"bus"})
        )

        with pytest.raises(FlowError, match="archive needs the bus transport"):
            resolve_flow(
                _source(transport="direct", archive=True), _settings(default="direct"), bus_only
            )

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


class TestAProfileThatDeploysNoSuchApp:
    """A tier that deploys nothing to run a stage must refuse the source at save.

    Accepting one writes an instance into the overlay that nothing reads, and the
    operator finds out by waiting for records that were never coming.
    """

    COMPOSE = ("docker-slim", "docker-single")
    KUBERNETES = ("slim", "single", "scale", "mesh")

    def test_a_fetched_source_is_refused_where_no_fetcher_is_deployed(self):
        with pytest.raises(FlowError, match="does not deploy dfe-fetcher"):
            resolve_flow(_fetched(), _settings(default="bus", profile="docker-slim"))

    def test_the_refusal_names_the_profile_that_gave_it(self):
        with pytest.raises(FlowError, match="the docker-slim profile"):
            resolve_flow(_fetched(), _settings(default="bus", profile="docker-slim"))

    @pytest.mark.parametrize("profile", KUBERNETES)
    def test_a_tier_that_deploys_one_still_resolves_it(self, profile):
        assert resolve_flow(_fetched(), _settings(default="bus", profile=profile)).input == (
            "dfe-fetcher-okta"
        )

    def test_a_deployment_naming_no_profile_refuses_nothing(self):
        # An unset profile is unknown, not empty, so a hand-run engine keeps
        # resolving every flow it did before.
        assert resolve_flow(_fetched(), _settings(default="bus")).input == "dfe-fetcher-okta"

    @pytest.mark.parametrize("profile", COMPOSE)
    def test_a_posted_source_is_untouched(self, profile):
        # The receiver is stack-wide and every tier deploys one.
        assert resolve_flow(_source(), _settings(default="bus", profile=profile)).origin == (
            "receiver"
        )

    def test_the_rule_is_manifest_data_rather_than_a_branch_per_app(self):
        # A transform restricted to another tier is refused the same way, which
        # is what stops this growing an app name per stage.
        restricted = dict(catalogue.APP_CATALOGUE)
        restricted["dfe-transform-vrl"] = replace(
            catalogue.descriptor("dfe-transform-vrl"), profiles=frozenset({"scale"})
        )

        with pytest.raises(FlowError, match="does not deploy dfe-transform-vrl"):
            resolve_flow(
                _source(transform={"engine": "vrl"}),
                _settings(default="bus", profile="slim"),
                restricted,
            )


class TestATargetThatRunsOneOfEachApp:
    """A per-config stage needs its own deployment, and Compose holds one of each.

    Compose declares its services in a committed file and creates none at run
    time, so the resident container is started idle and the first source to need
    it fills it. Where nothing renders the engine's config into that container
    the source would be saved and never run, so it is refused at save instead.
    """

    DOCKER = {"profile": "docker-single", "target": "docker"}
    WRITING = {**DOCKER, "app_config_dir": "/app/app-config"}

    def test_a_kubernetes_tier_runs_one_per_source(self):
        for name in ("okta", "cloudflare"):
            flow = resolve_flow(
                _fetched(name, fetcher={"source_type": name}),
                _settings(default="bus", profile="single", target="kubernetes"),
            )
            assert flow.input == f"dfe-fetcher-{name}"

    def test_a_fetched_source_is_refused_where_nothing_renders_the_config(self):
        with pytest.raises(FlowError, match="cannot configure one"):
            resolve_flow(_fetched(), _settings(default="bus", **self.DOCKER))

    def test_the_refusal_says_it_runs_one_and_names_what_turns_it_on(self):
        with pytest.raises(FlowError) as refused:
            resolve_flow(_fetched(), _settings(default="bus", **self.DOCKER))

        said = str(refused.value)
        assert "runs a single dfe-fetcher and it is deployed idle" in said
        assert "app-config directory" in said

    def test_a_transformed_source_is_refused_the_same_way(self):
        # The transform is per-config too, so the rule reads the manifest rather
        # than naming the fetcher.
        with pytest.raises(FlowError, match="cannot configure one"):
            resolve_flow(
                _source(transform={"engine": "vrl"}), _settings(default="bus", **self.DOCKER)
            )

    def test_a_docker_target_that_renders_its_app_config_takes_the_first_source(self):
        # The cap is one, not zero: the resident container now gets a config, so
        # the source it is given runs. The SECOND is refused against the deploy
        # repo, which the flow resolver never reads.
        flow = resolve_flow(_fetched(), _settings(default="bus", **self.WRITING))
        assert flow.input == "dfe-fetcher-okta"

    def test_a_transformed_source_is_taken_the_same_way(self):
        flow = resolve_flow(
            _source(transform={"engine": "vrl"}), _settings(default="bus", **self.WRITING)
        )
        assert flow.transform is not None
        assert flow.transform.app == "dfe-transform-vrl"

    def test_a_source_needing_only_stack_wide_apps_still_saves(self):
        # The receiver and the loader are one deployment each, so a plain posted
        # source is untouched by the cap.
        assert resolve_flow(_source(), _settings(default="bus", **self.DOCKER)).origin == "receiver"

    def test_an_app_declared_single_is_not_capped(self):
        single = dict(catalogue.APP_CATALOGUE)
        single["dfe-fetcher"] = replace(
            catalogue.descriptor("dfe-fetcher"), multiplicity=catalogue.Multiplicity.SINGLE
        )

        assert (
            resolve_flow(_fetched(), _settings(default="bus", **self.DOCKER), single).input
            == "dfe-fetcher-okta"
        )

    def test_a_target_nobody_named_refuses_nothing(self):
        assert (
            resolve_flow(_fetched(), _settings(default="bus", profile="docker-single")).input
            == "dfe-fetcher-okta"
        )
