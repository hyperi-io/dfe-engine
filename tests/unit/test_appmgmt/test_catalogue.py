#  Project:      dfe-engine
#  File:         tests/unit/test_appmgmt/test_catalogue.py
#  Purpose:      The manifest's flow facts, and the one place names and endpoints come from
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What apps.yaml says each app can do, read back as the engine reads it.

These assert the shipped manifest's VALUES, not that a parse succeeded: the
model's refusals are only as trustworthy as the transports declared here.
"""

from __future__ import annotations

import pytest

from dfe_engine.appmgmt import catalogue
from dfe_engine.appmgmt.catalogue import CatalogueError, load_catalogue
from dfe_engine.yaml_utils import yaml_dump


class TestShippedManifest:
    def test_the_apps_that_carry_direct_declare_it(self):
        carries_direct = {
            name for name, app in catalogue.APP_CATALOGUE.items() if app.carries("direct")
        }

        assert carries_direct == {
            "dfe-receiver",
            "dfe-loader",
            "dfe-fetcher",
            "dfe-transform-vrl",
            "dfe-transform-vector",
        }

    def test_vrl_and_vector_are_the_transforms_that_carry_direct(self):
        # Each runs a Push listener beside its bus consumer; elastic carries
        # the bus alone until its listener ships, which is a manifest edit.
        carriers = {
            name: app.carries("direct")
            for name, app in catalogue.APP_CATALOGUE.items()
            if name.startswith(catalogue.TRANSFORM_SERVICE_PREFIX)
        }

        assert carriers == {
            "dfe-transform-vrl": True,
            "dfe-transform-vector": True,
            "dfe-transform-elastic": False,
        }

    def test_the_archiver_reads_the_landing_topic_so_it_is_bus_only(self):
        assert catalogue.descriptor("dfe-archiver").transports == frozenset({"bus"})

    def test_elastic_is_bus_only_until_its_listener_ships(self):
        assert catalogue.descriptor("dfe-transform-elastic").transports == frozenset({"bus"})

    def test_hot_reload_is_declared_per_app(self):
        # A fact about each app, not a dial: the pods roll on any config change
        # regardless, so what an app could reload changes nothing about deploys.
        reloads = {name: app.hot_reload for name, app in catalogue.APP_CATALOGUE.items()}

        assert reloads == {
            "dfe-receiver": True,
            "dfe-loader": True,
            "dfe-archiver": False,
            "dfe-fetcher": True,
            "dfe-transform-vrl": False,
            "dfe-transform-vector": True,
            "dfe-transform-elastic": True,
        }

    def test_vector_reloads_its_transform_files_in_place(self):
        # It SIGHUPs Vector when only the transform files changed; the enrichment
        # tables are not watched, so they still roll.
        vector = catalogue.descriptor("dfe-transform-vector")
        reloads = {f.name: f.reload for f in vector.files}

        assert reloads["transforms"] == catalogue.ReloadMode.HOT
        assert reloads["enrichment"] == catalogue.ReloadMode.ROLL

    def test_vrl_holds_its_program_for_the_process_lifetime(self):
        vrl = catalogue.descriptor("dfe-transform-vrl")

        assert vrl.hot_reload is False
        assert {f.name: f.reload for f in vrl.files}["transforms"] == catalogue.ReloadMode.ROLL

    def test_the_apps_that_answer_on_a_push_listener_declare_it(self):
        # An app declares the endpoint when it ships the listener, so this set IS
        # the answer to "what can a direct source be sent to".
        listening = {
            name
            for name, app in catalogue.APP_CATALOGUE.items()
            if catalogue.PUSH_ENDPOINT in app.endpoints
        }

        assert listening == {"dfe-loader", "dfe-transform-vrl", "dfe-transform-vector"}

    def test_every_app_a_flow_can_send_to_on_direct_declares_its_listener(self):
        # A direct destination is built from this entry, so an app declaring the
        # transport without one refuses every source that names it. The receiver
        # and the fetcher carry direct only as senders, so nothing addresses them.
        destinations = {"dfe-loader"} | {
            name
            for name in catalogue.APP_CATALOGUE
            if name.startswith(catalogue.TRANSFORM_SERVICE_PREFIX)
        }
        undeliverable = sorted(
            name
            for name in destinations
            if catalogue.APP_CATALOGUE[name].carries("direct")
            and catalogue.PUSH_ENDPOINT not in catalogue.APP_CATALOGUE[name].endpoints
        )

        assert not undeliverable

    def test_the_loader_endpoint_is_the_one_the_charts_render(self):
        assert catalogue.push_endpoint(catalogue.descriptor("dfe-loader"), "") == (
            "http://dfe-loader:6000"
        )

    def test_the_manifest_declares_how_a_pool_behind_a_listener_is_addressed(self):
        # Both names are substituted, so neither the deployment's namespace nor
        # the pool's own name is baked into the pattern.
        assert catalogue.MESH_HOST_PATTERN == "{instance}-mesh.{mesh_namespace}.svc.cluster.local"

    def test_only_elastic_selects_a_compiled_in_program_by_name(self):
        variants = {
            name: app.variant_path
            for name, app in catalogue.APP_CATALOGUE.items()
            if app.variant_path
        }

        assert variants == {"dfe-transform-elastic": "config.source.name"}


class TestManifestParsing:
    def _manifest(self, tmp_path, app: dict, **top):
        path = tmp_path / "apps.yaml"
        yaml_dump({"apps": {"dfe-thing": app}, **top}, path)
        return path

    def test_an_app_that_declares_no_transport_is_bus_only(self, tmp_path):
        apps = load_catalogue(self._manifest(tmp_path, {"multiplicity": "single"}))

        assert apps["dfe-thing"].transports == frozenset({"bus"})

    def test_an_unknown_transport_is_refused_rather_than_read_as_a_missing_one(self, tmp_path):
        path = self._manifest(tmp_path, {"transports": ["bus", "gprc"]})

        with pytest.raises(CatalogueError, match="unknown transport"):
            load_catalogue(path)

    def test_an_empty_transport_list_is_refused(self, tmp_path):
        with pytest.raises(CatalogueError, match="non-empty list"):
            load_catalogue(self._manifest(tmp_path, {"transports": []}))

    def test_the_manifest_owns_the_port_a_stage_is_sent_to(self, tmp_path):
        apps = load_catalogue(
            self._manifest(
                tmp_path, {"multiplicity": "single", "endpoints": {"push": {"port": 7000}}}
            )
        )

        assert catalogue.push_endpoint(apps["dfe-thing"], "auth") == "http://dfe-thing:7000"
        assert catalogue.push_listen(apps["dfe-thing"]) == "0.0.0.0:7000"

    def test_a_declared_service_wins_over_the_apps_own_name(self, tmp_path):
        apps = load_catalogue(
            self._manifest(
                tmp_path, {"endpoints": {"push": {"port": 6000, "service": "dfe-thing-ingest"}}}
            )
        )

        assert catalogue.push_endpoint(apps["dfe-thing"], "auth") == "http://dfe-thing-ingest:6000"

    def test_an_app_with_no_endpoint_cannot_be_sent_to(self, tmp_path):
        # No entry means no listener, which is a different thing from a listener
        # on the usual port - so nothing invents an address for it.
        apps = load_catalogue(self._manifest(tmp_path, {"multiplicity": "single"}))

        with pytest.raises(catalogue.MissingEndpointError, match="declares no 'push' endpoint"):
            catalogue.push_endpoint(apps["dfe-thing"], "auth")

    def test_a_non_integer_endpoint_port_is_refused(self, tmp_path):
        path = self._manifest(tmp_path, {"endpoints": {"push": {"port": "six thousand"}}})

        with pytest.raises(CatalogueError, match="needs an integer port"):
            load_catalogue(path)

    def test_a_variant_path_outside_every_derived_block_is_refused(self, tmp_path):
        path = self._manifest(
            tmp_path,
            {
                "multiplicity": "per_config",
                "variant_path": "config.elsewhere.name",
                "routing": {
                    "compiler": "transform",
                    "scope": "instance",
                    "values_paths": {"source": "config.source"},
                },
            },
        )

        with pytest.raises(CatalogueError, match="outside every derived block"):
            load_catalogue(path)

    def test_an_empty_values_paths_mapping_is_refused(self, tmp_path):
        path = self._manifest(tmp_path, {"routing": {"compiler": "receiver", "values_paths": {}}})

        with pytest.raises(CatalogueError, match="non-empty mapping"):
            load_catalogue(path)

    def test_a_manifest_with_no_mesh_block_addresses_nothing_that_way(self, tmp_path):
        assert catalogue.load_mesh(self._manifest(tmp_path, {})) == ""

    def test_a_host_pattern_naming_anything_else_is_refused_at_load(self, tmp_path):
        # It renders an address a sender dials, so a name nothing substitutes
        # would fail at the send rather than here.
        path = self._manifest(tmp_path, {}, mesh={"host_pattern": "{pool}.{cluster}"})

        with pytest.raises(CatalogueError, match="may name only"):
            catalogue.load_mesh(path)

    def test_a_mesh_that_is_not_a_mapping_is_refused(self, tmp_path):
        path = self._manifest(tmp_path, {}, mesh="{instance}-mesh")

        with pytest.raises(CatalogueError, match="'mesh' must be a mapping"):
            catalogue.load_mesh(path)


class TestNaming:
    def test_an_instance_is_named_for_its_app_and_its_source(self):
        vrl = catalogue.descriptor("dfe-transform-vrl")

        assert catalogue.instance_name(vrl, "auth") == "dfe-transform-vrl-auth"
        assert catalogue.instance_component(vrl, "auth") == "transform-vrl-auth"

    def test_a_stack_wide_app_answers_on_its_own_service(self):
        loader = catalogue.descriptor("dfe-loader")

        assert catalogue.push_endpoint(loader, "auth") == "http://dfe-loader:6000"

    def test_a_per_source_app_answers_on_its_instance_service(self):
        vrl = catalogue.descriptor("dfe-transform-vrl")

        assert catalogue.push_endpoint(vrl, "auth") == "http://dfe-transform-vrl-auth:6000"

    def test_transform_service_is_the_inverse_of_the_engine_name(self):
        for engine in catalogue.transform_engines():
            assert catalogue.transform_service(engine) in catalogue.APP_CATALOGUE


class TestMeshAddressing:
    """A deployment whose pools sit behind listeners, addressed by the same rule."""

    NAMESPACE = "envoy-gateway-system"

    def test_a_stack_wide_pool_is_dialled_at_its_listener_alias(self):
        loader = catalogue.descriptor("dfe-loader")

        assert catalogue.push_endpoint(loader, "", self.NAMESPACE) == (
            f"http://dfe-loader-mesh.{self.NAMESPACE}.svc.cluster.local:6000"
        )

    def test_a_per_source_pool_carries_its_source_into_the_alias(self):
        vrl = catalogue.descriptor("dfe-transform-vrl")

        assert catalogue.push_endpoint(vrl, "auth", self.NAMESPACE) == (
            f"http://dfe-transform-vrl-auth-mesh.{self.NAMESPACE}.svc.cluster.local:6000"
        )

    def test_the_port_is_the_declared_one_either_way(self):
        # The alias publishes the pool's own push port, so only the host moves.
        vrl = catalogue.descriptor("dfe-transform-vrl")

        assert catalogue.push_endpoint(vrl, "auth").endswith(":6000")
        assert catalogue.push_endpoint(vrl, "auth", self.NAMESPACE).endswith(":6000")

    def test_a_pod_binds_its_own_port_whatever_fronts_it(self):
        assert catalogue.push_listen(catalogue.descriptor("dfe-transform-vrl")) == "0.0.0.0:6000"

    def test_a_manifest_with_no_pattern_refuses_to_invent_an_address(self, monkeypatch):
        monkeypatch.setattr(catalogue, "MESH_HOST_PATTERN", "")

        with pytest.raises(CatalogueError, match=r"no mesh\.host_pattern"):
            catalogue.push_endpoint(catalogue.descriptor("dfe-loader"), "", self.NAMESPACE)
