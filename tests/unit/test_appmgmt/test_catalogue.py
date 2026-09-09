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

        assert carries_direct == {"dfe-receiver", "dfe-loader", "dfe-fetcher"}

    def test_no_transform_carries_direct_until_one_ships_a_listener(self):
        transforms = {
            name: app
            for name, app in catalogue.APP_CATALOGUE.items()
            if name.startswith(catalogue.TRANSFORM_SERVICE_PREFIX)
        }

        assert transforms
        assert not any(app.carries("direct") for app in transforms.values())

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

    def test_only_the_loader_answers_on_a_push_listener_today(self):
        # An app declares the endpoint when it ships the listener, so this set IS
        # the answer to "what can a direct source be sent to".
        listening = {
            name
            for name, app in catalogue.APP_CATALOGUE.items()
            if catalogue.PUSH_ENDPOINT in app.endpoints
        }

        assert listening == {"dfe-loader"}

    def test_the_loader_endpoint_is_the_one_the_charts_render(self):
        assert catalogue.push_endpoint(catalogue.descriptor("dfe-loader"), "") == (
            "http://dfe-loader:6000"
        )

    def test_only_elastic_selects_a_compiled_in_program_by_name(self):
        variants = {
            name: app.variant_path
            for name, app in catalogue.APP_CATALOGUE.items()
            if app.variant_path
        }

        assert variants == {"dfe-transform-elastic": "config.source.name"}


class TestManifestParsing:
    def _manifest(self, tmp_path, app: dict):
        path = tmp_path / "apps.yaml"
        yaml_dump({"apps": {"dfe-thing": app}}, path)
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


class TestNaming:
    def test_an_instance_is_named_for_its_app_and_its_source(self):
        vrl = catalogue.descriptor("dfe-transform-vrl")

        assert catalogue.instance_name(vrl, "auth") == "dfe-transform-vrl-auth"
        assert catalogue.instance_component(vrl, "auth") == "transform-vrl-auth"

    def test_a_stack_wide_app_answers_on_its_own_service(self):
        loader = catalogue.descriptor("dfe-loader")

        assert catalogue.push_endpoint(loader, "auth") == "http://dfe-loader:6000"

    def test_a_per_source_app_answers_on_its_instance_service(self):
        from dataclasses import replace

        vrl = replace(
            catalogue.descriptor("dfe-transform-vrl"),
            endpoints={catalogue.PUSH_ENDPOINT: catalogue.AppEndpoint(port=6000)},
        )

        assert catalogue.push_endpoint(vrl, "auth") == "http://dfe-transform-vrl-auth:6000"

    def test_transform_service_is_the_inverse_of_the_engine_name(self):
        for engine in catalogue.transform_engines():
            assert catalogue.transform_service(engine) in catalogue.APP_CATALOGUE
