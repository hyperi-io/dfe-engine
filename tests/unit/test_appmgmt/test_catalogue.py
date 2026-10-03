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
            "dfe-archiver",
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

    def test_the_archiver_takes_the_record_on_either_transport(self):
        # On the bus it reads the landing topics; on direct the sender fans the
        # record out to its Push listener beside the loader.
        archiver = catalogue.descriptor("dfe-archiver")

        assert archiver.transports == frozenset({"bus", "direct"})
        assert archiver.endpoints[catalogue.PUSH_ENDPOINT].port == 6000

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
            "dfe-transform-elastic": False,
            "culvert": False,
            "dfe-engine": False,
            "dfe-ui": False,
            "hyperdx": False,
        }

    def test_the_loader_names_the_key_that_turns_its_watcher_on(self):
        # dfe-loader's HotReloadConfig::enabled defaults false; every other
        # reloading app watches without being asked.
        settings = {
            name: app.reload_setting
            for name, app in catalogue.APP_CATALOGUE.items()
            if app.reload_setting
        }

        assert settings == {"dfe-loader": "config.hot_reload.enabled"}

    def test_the_receiver_names_the_destinations_it_builds_at_startup(self):
        # It reloads its routing in place but opens one sink per destination once,
        # so a new direct destination needs a restart although the app reloads.
        receiver = catalogue.descriptor("dfe-receiver")

        assert receiver.hot_reload is True
        assert "config.destinations" in receiver.restart_paths
        assert "config.routing" not in receiver.restart_paths

    def test_every_block_the_engine_writes_into_a_fetcher_reports_a_restart(self):
        # A fetcher reload re-reads a source's filter and interval but adds no
        # source and rebuilds no output, so a Compose operator told nothing would
        # keep running the old source.
        fetcher = catalogue.descriptor("dfe-fetcher")

        written = set(fetcher.routing_paths.values())

        assert fetcher.hot_reload is True
        assert written
        assert written <= set(fetcher.restart_paths)

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

        assert listening == {
            "dfe-loader",
            "dfe-archiver",
            "dfe-transform-vrl",
            "dfe-transform-vector",
        }

    def test_every_app_a_flow_can_send_to_on_direct_declares_its_listener(self):
        # A direct destination is built from this entry, so an app declaring the
        # transport without one refuses every source that names it. The receiver
        # and the fetcher carry direct only as senders, so nothing addresses them.
        destinations = {"dfe-loader", "dfe-archiver"} | {
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

    def test_the_edge_door_carries_no_records(self):
        # It is what an appliance dials before it posts to the receiver, so no
        # source may ever name it, which is a different statement from the bus.
        assert catalogue.descriptor("culvert").transports == frozenset()

    def test_where_each_app_may_be_deployed_comes_off_the_manifest(self):
        restricted = {
            name: sorted(app.profiles)
            for name, app in catalogue.APP_CATALOGUE.items()
            if app.profiles
        }

        # docker-slim runs the core data path alone, so the fetcher may not be
        # deployed there and a source asking to be fetched is refused.
        assert restricted == {
            "culvert": ["mesh", "scale"],
            "dfe-fetcher": ["docker-single", "mesh", "scale", "single", "slim"],
        }

    def test_the_apps_nothing_deploys_by_default_are_the_per_source_ones_and_the_door(self):
        # An instance of a per-source app arrives with its source, and the edge
        # door is dialled by an appliance fleet a deployment may not have, so
        # neither is seeded by a profile. The fetcher and the vrl and elastic
        # transforms are the exception: each declares idle_when, so the tiers in
        # its default_in seed one idle and the source it is later given fills it.
        optional = {name for name, app in catalogue.APP_CATALOGUE.items() if app.optional}

        assert optional == {
            "dfe-transform-vector",
            "culvert",
        }

    def test_the_stack_wide_apps_are_deployed_wherever_they_may_be(self):
        # The core data path plus the two consoles: a DFE without them is not one,
        # so none of them waits for an operator to turn it on.
        always = {
            name
            for name, app in catalogue.APP_CATALOGUE.items()
            if not app.optional and app.multiplicity is catalogue.Multiplicity.SINGLE
        }

        assert always == {
            "dfe-receiver",
            "dfe-loader",
            "dfe-archiver",
            "dfe-engine",
            "dfe-ui",
            "hyperdx",
        }

    def test_no_app_is_deployed_by_default_somewhere_it_may_not_be_deployed(self):
        # default_in is a subset of the offer, so a profile cannot seed an app the
        # same manifest says may not run there. Empty profiles means every profile.
        overreaching = {
            name: sorted(app.default_in - app.profiles)
            for name, app in catalogue.APP_CATALOGUE.items()
            if app.profiles and app.default_in and app.default_in - app.profiles
        }

        assert overreaching == {}

    def test_the_manifest_declares_what_unconfigured_looks_like_per_app(self):
        # Read and reported, never evaluated: the apps carry the same predicate.
        declared = {
            name: app.idle_when for name, app in catalogue.APP_CATALOGUE.items() if app.idle_when
        }

        assert declared == {
            "dfe-archiver": (
                "config.archive.destination",
                "config.kafka.topics",
                "config.kafka.topic_include",
            ),
            "dfe-fetcher": (
                "config.sources",
                "config.extractors.containers",
                "config.ingest.enabled",
            ),
            "dfe-transform-vrl": ("config.source.topics",),
            "dfe-transform-elastic": (
                "config.source.name",
                "config.source.topics",
            ),
        }

    def test_only_elastic_selects_a_compiled_in_program_by_name(self):
        variants = {
            name: app.variant_path
            for name, app in catalogue.APP_CATALOGUE.items()
            if app.variant_path
        }

        assert variants == {"dfe-transform-elastic": "config.source.name"}

    def test_elastic_is_the_one_app_shipping_a_source_catalogue(self):
        shipping = {
            name: app.catalogue for name, app in catalogue.APP_CATALOGUE.items() if app.catalogue
        }

        assert set(shipping) == {"dfe-transform-elastic"}
        binding = shipping["dfe-transform-elastic"]
        assert binding.file == "sources.yaml"
        assert binding.entries_key == "sources"
        assert binding.variant("okta", "default") == "filebeat.okta.default"

    def test_elastic_names_ecs_and_a_derived_and_vendor_layer_per_stream(self):
        binding = catalogue.descriptor("dfe-transform-elastic").catalogue
        assert binding is not None
        layers = binding.schema_layers

        assert layers is not None
        assert layers.meta_schema == "meta/elastic/ecs"
        assert layers.derived("cisco_ios", "log") == "derived/cisco_ios/log"
        assert layers.additional("cisco_ios", "log") == "additional/cisco_ios/log"

    def test_the_mounted_catalogue_finds_its_owner_by_filename(self):
        assert catalogue.catalogue_app("sources.yaml").service == "dfe-transform-elastic"

    def test_a_catalogue_no_app_ships_is_refused_with_what_is_declared(self):
        with pytest.raises(CatalogueError, match=r"declared: sources\.yaml"):
            catalogue.catalogue_app("elsewhere.yaml")

    def test_a_package_the_fetcher_names_the_same_way_needs_no_mapping(self):
        assert catalogue.source_type_for_package("okta") == "okta"
        assert catalogue.source_type_for_package("aws") == "aws"

    def test_the_manifest_carries_the_names_the_two_spell_apart(self):
        assert catalogue.source_type_for_package("1password") == "onepassword"
        assert catalogue.source_type_for_package("cisco_duo") == "duo"
        assert catalogue.source_type_for_package("golang") == "go_modules"
        assert catalogue.source_type_for_package("o365") == "m365"

    def test_a_package_no_family_polls_has_none(self):
        assert catalogue.source_type_for_package("zoom") is None

    def test_the_fetchers_instance_maps_are_its_keyed_families(self):
        # dfe-fetcher's SourcesConfig carries rest, db and file as maps keyed by
        # instance id, and every other family as one flat block.
        assert catalogue.descriptor("dfe-fetcher").keyed_source_types == frozenset(
            {"rest", "db", "file"}
        )

    def test_hyperdx_is_the_one_app_a_console_labels_by_another_name(self):
        labelled = {
            name: app.display_name
            for name, app in catalogue.APP_CATALOGUE.items()
            if app.display_name
        }

        assert labelled == {"hyperdx": "Search"}


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

    def test_an_empty_transport_list_is_an_app_that_carries_no_records(self, tmp_path):
        apps = load_catalogue(self._manifest(tmp_path, {"transports": []}))

        assert apps["dfe-thing"].transports == frozenset()
        assert not apps["dfe-thing"].carries("bus")
        assert not apps["dfe-thing"].carries("direct")

    def test_a_transport_value_that_is_not_a_list_is_refused(self, tmp_path):
        with pytest.raises(CatalogueError, match="transports must be a list"):
            load_catalogue(self._manifest(tmp_path, {"transports": "bus"}))

    def test_an_app_naming_neither_key_is_in_every_profile_by_default(self, tmp_path):
        apps = load_catalogue(self._manifest(tmp_path, {"multiplicity": "single"}))
        app = apps["dfe-thing"]

        assert app.profiles == frozenset()
        assert app.default_in is None
        assert app.optional is False
        assert app.offered_in("scale")

    def test_an_app_may_be_deployed_only_in_the_profiles_it_names(self, tmp_path):
        apps = load_catalogue(
            self._manifest(tmp_path, {"multiplicity": "single", "profiles": ["scale", "mesh"]})
        )
        app = apps["dfe-thing"]

        assert app.profiles == frozenset({"scale", "mesh"})
        assert app.offered_in("scale")
        assert not app.offered_in("slim")
        # Nothing says otherwise, so it is deployed wherever it may be.
        assert app.optional is False

    def test_an_empty_default_in_is_what_makes_an_app_optional(self, tmp_path):
        apps = load_catalogue(
            self._manifest(
                tmp_path,
                {
                    "multiplicity": "single",
                    "profiles": ["scale", "mesh"],
                    "default_in": [],
                },
            )
        )
        app = apps["dfe-thing"]

        assert app.default_in == frozenset()
        assert app.optional is True
        assert app.offered_in("mesh")
        assert not app.offered_in("slim")

    def test_an_app_can_be_deployable_further_than_it_is_deployed(self, tmp_path):
        apps = load_catalogue(
            self._manifest(
                tmp_path,
                {"profiles": ["single", "scale", "mesh"], "default_in": ["scale"]},
            )
        )
        app = apps["dfe-thing"]

        assert app.optional is False
        assert app.default_in == frozenset({"scale"})
        assert app.offered_in("single")

    def test_a_caller_that_names_no_profile_is_offered_everything(self, tmp_path):
        # Nothing tells the engine which profile deployed it, so hiding an app on a
        # blank answer would hide it in every deployment.
        apps = load_catalogue(self._manifest(tmp_path, {"profiles": ["scale"], "default_in": []}))

        assert apps["dfe-thing"].offered_in("")

    def test_an_empty_profile_list_is_refused(self, tmp_path):
        # Written and left empty says "deployable nowhere"; the absent key already
        # says "everywhere", so the empty list can only be a mistake. default_in is
        # the key where an empty list means something.
        with pytest.raises(CatalogueError, match="profiles must be a non-empty list"):
            load_catalogue(self._manifest(tmp_path, {"profiles": []}))

    def test_profile_keys_that_are_not_lists_are_refused(self, tmp_path):
        with pytest.raises(CatalogueError, match="default_in must be a list"):
            load_catalogue(self._manifest(tmp_path, {"default_in": "scale"}))

    def test_an_app_naming_no_display_name_is_labelled_by_its_id(self, tmp_path):
        apps = load_catalogue(self._manifest(tmp_path, {"multiplicity": "single"}))

        assert apps["dfe-thing"].display_name == ""

    def test_the_display_name_is_read_as_the_manifest_gives_it(self, tmp_path):
        apps = load_catalogue(self._manifest(tmp_path, {"display_name": "Thing Console"}))

        assert apps["dfe-thing"].display_name == "Thing Console"

    @pytest.mark.parametrize("value", [True, 2024, "", "   ", ["Search"]])
    def test_a_display_name_that_is_not_a_non_blank_string_is_refused(self, tmp_path, value):
        with pytest.raises(CatalogueError, match="display_name must be a non-empty string"):
            load_catalogue(self._manifest(tmp_path, {"display_name": value}))

    def test_an_app_naming_no_idle_condition_always_has_work(self, tmp_path):
        apps = load_catalogue(self._manifest(tmp_path, {"multiplicity": "single"}))

        assert apps["dfe-thing"].idle_when == ()

    def test_the_idle_paths_are_read_in_the_order_the_manifest_gives_them(self, tmp_path):
        # The order is the app's own, so it is kept rather than sorted into one
        # the app's work_state does not share.
        path = self._manifest(tmp_path, {"idle_when": ["config.sources", "config.ingest.enabled"]})

        assert load_catalogue(path)["dfe-thing"].idle_when == (
            "config.sources",
            "config.ingest.enabled",
        )

    def test_a_reload_setting_on_an_app_that_does_not_reload_is_refused(self, tmp_path):
        path = self._manifest(tmp_path, {"reload_setting": "config.hot_reload.enabled"})

        with pytest.raises(CatalogueError, match="declares hot_reload false"):
            load_catalogue(path)

    def test_a_reload_setting_outside_the_config_block_is_refused(self, tmp_path):
        path = self._manifest(tmp_path, {"hot_reload": True, "reload_setting": "hot_reload"})

        with pytest.raises(CatalogueError, match=r"must be a config\. overlay path"):
            load_catalogue(path)

    def test_an_idle_when_that_is_not_a_list_is_refused(self, tmp_path):
        with pytest.raises(CatalogueError, match="idle_when must be a list"):
            load_catalogue(self._manifest(tmp_path, {"idle_when": "config.sources"}))

    def test_an_app_naming_no_restart_paths_binds_nothing_at_startup(self, tmp_path):
        apps = load_catalogue(self._manifest(tmp_path, {"hot_reload": True}))

        assert apps["dfe-thing"].restart_paths == ()

    def test_restart_paths_are_read_in_the_order_the_manifest_gives_them(self, tmp_path):
        path = self._manifest(
            tmp_path, {"hot_reload": True, "restart_paths": ["config.sinks", "config.listen"]}
        )

        assert load_catalogue(path)["dfe-thing"].restart_paths == ("config.sinks", "config.listen")

    def test_restart_paths_on_an_app_that_does_not_reload_are_refused(self, tmp_path):
        # Every change restarts such an app already, so the list could only mislead.
        path = self._manifest(tmp_path, {"restart_paths": ["config.sinks"]})

        with pytest.raises(CatalogueError, match="declares hot_reload false"):
            load_catalogue(path)

    def test_a_restart_path_outside_the_config_block_is_refused(self, tmp_path):
        path = self._manifest(tmp_path, {"hot_reload": True, "restart_paths": ["replicaCount"]})

        with pytest.raises(CatalogueError, match=r"must be a config\. overlay path"):
            load_catalogue(path)

    def test_restart_paths_that_are_not_a_list_are_refused(self, tmp_path):
        path = self._manifest(tmp_path, {"hot_reload": True, "restart_paths": "config.sinks"})

        with pytest.raises(CatalogueError, match="restart_paths must be a list"):
            load_catalogue(path)

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
        assert catalogue.push_address(apps["dfe-thing"], "auth") == "dfe-thing-ingest:6000"

    def test_an_app_with_no_endpoint_cannot_be_sent_to(self, tmp_path):
        # No entry means no listener, which is a different thing from a listener
        # on the usual port - so nothing invents an address for it.
        apps = load_catalogue(self._manifest(tmp_path, {"multiplicity": "single"}))

        with pytest.raises(catalogue.MissingEndpointError, match="declares no 'push' endpoint"):
            catalogue.push_endpoint(apps["dfe-thing"], "auth")
        with pytest.raises(catalogue.MissingEndpointError, match="declares no 'push' endpoint"):
            catalogue.push_address(apps["dfe-thing"], "auth")

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

    def test_a_catalogue_missing_a_key_names_what_it_needs(self, tmp_path):
        path = self._manifest(tmp_path, {"catalogue": {"file": "sources.yaml"}})

        with pytest.raises(CatalogueError, match="file, entries_key and variant_pattern"):
            load_catalogue(path)

    def test_a_variant_pattern_naming_an_unknown_placeholder_is_refused(self, tmp_path):
        path = self._manifest(
            tmp_path,
            {
                "catalogue": {
                    "file": "sources.yaml",
                    "entries_key": "sources",
                    "variant_pattern": "{vendor}.{entry}",
                }
            },
        )

        with pytest.raises(CatalogueError, match="takes only"):
            load_catalogue(path)

    @staticmethod
    def _catalogue(**layers: str) -> dict:
        return {
            "catalogue": {
                "file": "sources.yaml",
                "entries_key": "sources",
                "variant_pattern": "{entry}.{transform}",
                **layers,
            }
        }

    def test_a_catalogue_naming_no_schema_layers_loads_with_none(self, tmp_path):
        binding = load_catalogue(self._manifest(tmp_path, self._catalogue()))["dfe-thing"].catalogue

        assert binding is not None
        assert binding.schema_layers is None

    def test_some_schema_layers_without_the_rest_are_refused_by_name(self, tmp_path):
        path = self._manifest(tmp_path, self._catalogue(meta_schema="meta/elastic/ecs"))

        with pytest.raises(CatalogueError, match="without derived_pattern, additional_pattern"):
            load_catalogue(path)

    def test_a_meta_schema_with_a_file_extension_is_refused(self, tmp_path):
        # A derived schema names its base extensionless, so this one could never match.
        path = self._manifest(
            tmp_path,
            self._catalogue(
                meta_schema="meta/elastic/ecs.yaml",
                derived_pattern="derived/{package}/{data_stream}",
                additional_pattern="additional/{package}/{data_stream}",
            ),
        )

        with pytest.raises(CatalogueError, match="without a file extension"):
            load_catalogue(path)

    def test_a_layer_pattern_naming_an_unknown_placeholder_is_refused(self, tmp_path):
        path = self._manifest(
            tmp_path,
            self._catalogue(
                meta_schema="meta/elastic/ecs",
                derived_pattern="derived/{vendor}/{data_stream}",
                additional_pattern="additional/{package}/{data_stream}",
            ),
        )

        with pytest.raises(CatalogueError, match=r"derived_pattern .* takes only \{package\}"):
            load_catalogue(path)

    def test_a_catalogue_package_naming_a_family_the_app_lacks_is_refused(self, tmp_path):
        path = self._manifest(
            tmp_path,
            {"source_types": ["okta"], "catalogue_packages": {"duo": ["cisco_duo"]}},
        )

        with pytest.raises(CatalogueError, match="not one of its source_types"):
            load_catalogue(path)

    def test_an_empty_package_list_is_refused(self, tmp_path):
        path = self._manifest(
            tmp_path, {"source_types": ["duo"], "catalogue_packages": {"duo": []}}
        )

        with pytest.raises(CatalogueError, match="non-empty list of packages"):
            load_catalogue(path)

    def test_an_app_naming_no_keyed_families_composes_every_family_flat(self, tmp_path):
        apps = load_catalogue(self._manifest(tmp_path, {"source_types": ["rest", "okta"]}))

        assert apps["dfe-thing"].keyed_source_types == frozenset()

    def test_the_keyed_families_are_read_as_the_manifest_gives_them(self, tmp_path):
        path = self._manifest(
            tmp_path, {"source_types": ["rest", "okta"], "keyed_source_types": ["rest"]}
        )

        assert load_catalogue(path)["dfe-thing"].keyed_source_types == frozenset({"rest"})

    def test_a_keyed_family_the_app_lacks_is_refused(self, tmp_path):
        path = self._manifest(tmp_path, {"source_types": ["okta"], "keyed_source_types": ["rest"]})

        with pytest.raises(CatalogueError, match=r"keyed_source_types names 'rest'.*source_types"):
            load_catalogue(path)

    def test_keyed_families_that_are_not_a_list_are_refused(self, tmp_path):
        path = self._manifest(tmp_path, {"source_types": ["rest"], "keyed_source_types": "rest"})

        with pytest.raises(CatalogueError, match="keyed_source_types must be a list"):
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
        vrl = catalogue.descriptor("dfe-transform-vrl")

        assert catalogue.push_endpoint(vrl, "auth") == "http://dfe-transform-vrl-auth:6000"

    def test_the_push_address_is_the_endpoint_without_its_scheme(self):
        loader = catalogue.descriptor("dfe-loader")
        vrl = catalogue.descriptor("dfe-transform-vrl")

        assert catalogue.push_address(loader) == "dfe-loader:6000"
        assert catalogue.push_address(vrl, "auth") == "dfe-transform-vrl-auth:6000"
        assert (
            catalogue.push_endpoint(vrl, "auth") == f"http://{catalogue.push_address(vrl, 'auth')}"
        )

    def test_transform_service_is_the_inverse_of_the_engine_name(self):
        for engine in catalogue.transform_engines():
            assert catalogue.transform_service(engine) in catalogue.APP_CATALOGUE

    def test_a_transform_app_carries_the_engine_a_source_names(self):
        assert catalogue.descriptor("dfe-transform-vrl").transform_engine == "vrl"
        assert catalogue.descriptor("dfe-transform-vector").transform_engine == "vector"
        assert catalogue.descriptor("dfe-transform-elastic").transform_engine == "elastic"

    def test_an_app_that_is_not_a_transform_carries_no_engine(self):
        for service in ("dfe-receiver", "dfe-loader", "dfe-archiver", "dfe-fetcher"):
            assert catalogue.descriptor(service).transform_engine == ""

    def test_the_engine_set_is_exactly_what_the_descriptors_carry(self):
        # One derivation: a picker reading the per-app field and the write-path
        # validator reading the set cannot disagree about what a transform is.
        assert catalogue.transform_engines() == {
            app.transform_engine for app in catalogue.APP_CATALOGUE.values() if app.transform_engine
        }


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
