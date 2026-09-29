#  Project:      dfe-engine
#  File:         tests/unit/test_appmgmt/test_routing.py
#  Purpose:      Tests for delivering source-derived routing into an overlay
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Routing is derived, so the overlay is compared to the sources, never trusted.

The regression these guard is the one found in the field: a deployed receiver
whose config was an empty document, running on built-in defaults while every
source rule ever defined was ignored.

The compiled blocks are asserted as EXACT dicts per origin, per transport, and
with and without a transform, because the whole point of compiling is that a
source definition alone decides where its records go.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from dfe_engine.appmgmt import catalogue, instances, routing
from dfe_engine.source.alignment import MixedConnectorTypesError

from .conftest import FakeRegistry

RECEIVER = "dfe-receiver"
LOADER = "dfe-loader"
FETCHER = "dfe-fetcher"
VRL = "dfe-transform-vrl"
ARCHIVER = "dfe-archiver"
# Carries no records at all, so nothing about it is derived from the sources.
UI = "dfe-ui"

LOADER_ENDPOINT = "http://dfe-loader:6000"
ARCHIVER_ENDPOINT = "http://dfe-archiver:6000"
VRL_AUTH_ENDPOINT = "http://dfe-transform-vrl-auth:6000"


def _matched(name: str = "auth", **fields):
    from dfe_engine.source.models import Source

    doc: dict = {
        "source": name,
        "state": "active",
        "match": {"field": "_json.app", "operator": "equals", "value": name},
        **fields,
    }
    return Source.model_validate(doc)


def _fetched(name: str = "okta-audit", **fields):
    from dfe_engine.source.models import Source

    doc: dict = {
        "source": name,
        "state": "active",
        "fetcher": {
            "source_type": "okta",
            "config": {"tenant_url": "https://example.okta.com"},
            **fields.pop("fetcher", {}),
        },
        **fields,
    }
    return Source.model_validate(doc)


@pytest.fixture
def direct_transforms():
    """The catalogue as it is once a transform ships its Push listener.

    Declaring the transport and the endpoint is the WHOLE of what a transform
    gains one, so a test of the direct form is a manifest edit, never a stub.
    """
    original = dict(catalogue.APP_CATALOGUE)
    catalogue.APP_CATALOGUE[VRL] = replace(
        original[VRL],
        transports=frozenset({"bus", "direct"}),
        endpoints={catalogue.PUSH_ENDPOINT: catalogue.AppEndpoint(port=6000)},
    )
    yield catalogue.APP_CATALOGUE
    catalogue.APP_CATALOGUE.clear()
    catalogue.APP_CATALOGUE.update(original)


@pytest.fixture
def source_registry():
    """One receiver-matched source, as the shipped filebeat flow uses."""
    return FakeRegistry([_matched("filebeat")])


@pytest.fixture
def fetched_registry():
    return FakeRegistry([_fetched(), _matched("filebeat")])


@pytest.fixture
def overlay():
    return instances.initial_overlay(instances.instance_of(RECEIVER, "default"))


class TestManifest:
    def test_the_receiver_owns_its_rules_and_its_destinations(self):
        app = catalogue.descriptor(RECEIVER)
        assert app.has_compiled_routing is True
        assert app.routing_compiler == "receiver"
        assert app.routing_paths == {
            "routing": "config.routing",
            "destinations": "config.destinations",
        }

    def test_the_loader_declares_its_own(self):
        app = catalogue.descriptor(LOADER)
        assert (app.routing_compiler, app.routing_paths) == (
            "loader",
            {
                "routing": "config.routing",
                "capture": "config.metadata.table_capture_modes",
            },
        )

    def test_the_fetcher_owns_what_it_polls_and_where_it_sends(self):
        app = catalogue.descriptor(FETCHER)
        assert app.routing_is_per_instance is True
        assert app.routing_paths == {"sources": "config.sources", "output": "config.output"}

    def test_a_transform_is_instance_routed_like_a_fetcher(self):
        app = catalogue.descriptor(VRL)
        assert (app.routing_compiler, app.routing_is_per_instance) == ("transform", True)
        assert {"source", "sink"} <= set(app.routing_paths)

    def test_the_archiver_owns_the_topics_it_reads(self):
        app = catalogue.descriptor(ARCHIVER)
        assert (app.routing_compiler, app.routing_paths) == (
            "archiver",
            {"topics": "config.kafka.topics"},
        )

    def test_an_app_without_derived_routing_says_so(self):
        assert catalogue.descriptor(UI).has_compiled_routing is False

    def test_every_declared_compiler_is_implemented(self):
        # A manifest naming a compiler the engine does not have would fail at
        # request time on a deployed cluster, which is the worst place to find it.
        declared = {
            app.routing_compiler for app in catalogue.APP_CATALOGUE.values() if app.routing_compiler
        }
        assert declared <= set(routing.compilers())

    def test_every_declared_variant_path_lands_in_a_derived_block(self):
        for app in catalogue.APP_CATALOGUE.values():
            if app.variant_path:
                assert app.block_for(app.variant_path)[0] in app.routing_paths


class TestStatus:
    def test_an_empty_overlay_is_absent_not_merely_drifted(
        self, overlay, source_registry, settings
    ):
        app = catalogue.descriptor(RECEIVER)
        found = routing.status(app, overlay, source_registry, settings)
        assert found.absent is True
        assert found.drift is True

    def test_a_synced_overlay_reports_neither(self, overlay, source_registry, settings):
        app = catalogue.descriptor(RECEIVER)
        assert routing.sync(app, overlay, source_registry, settings) is True
        found = routing.status(app, overlay, source_registry, settings)
        assert (found.drift, found.absent) == (False, False)

    def test_a_hand_edit_over_derived_routing_is_drift(self, overlay, source_registry, settings):
        app = catalogue.descriptor(RECEIVER)
        routing.sync(app, overlay, source_registry, settings)
        overlay["config"]["routing"]["default_source"] = "hand_edited"
        assert routing.status(app, overlay, source_registry, settings).drift is True

    def test_a_hand_edit_over_the_destinations_is_drift(self, overlay, source_registry, settings):
        app = catalogue.descriptor(RECEIVER)
        routing.sync(app, overlay, source_registry, settings)
        overlay["config"]["destinations"]["default"] = "loader"
        found = routing.status(app, overlay, source_registry, settings)
        assert found.drift is True
        assert routing.sync(app, overlay, source_registry, settings) is True
        assert overlay["config"]["destinations"]["default"] == "kafka"

    def test_syncing_twice_is_not_a_change(self, overlay, source_registry, settings):
        app = catalogue.descriptor(RECEIVER)
        assert routing.sync(app, overlay, source_registry, settings) is True
        assert routing.sync(app, overlay, source_registry, settings) is False

    def test_an_app_without_derived_routing_refuses(self, source_registry, settings):
        with pytest.raises(routing.RoutingNotCompiledError):
            routing.compile_for(catalogue.descriptor(UI), source_registry, settings)

    def test_a_block_the_compile_stops_emitting_is_removed(self, settings, direct_settings):
        # Moving a source between transports must not leave the other
        # transport's keys beside the new ones.
        app = catalogue.descriptor(FETCHER)
        doc = instances.initial_overlay(instances.instance_of(FETCHER, "okta-audit"))
        routing.apply(app, doc, {"sources": {"okta": {}}, "output": {"type": "kafka"}})
        routing.apply(app, doc, {"sources": {"okta": {}}})
        assert "output" not in doc["config"]


class TestUnknownCompiler:
    def test_an_unknown_compiler_raises_rather_than_returning_nothing(
        self, source_registry, settings
    ):
        app = replace(catalogue.descriptor(RECEIVER), routing_compiler="nonexistent")
        with pytest.raises(routing.UnknownRoutingCompilerError):
            routing.compile_for(app, source_registry, settings)

    def test_a_block_the_manifest_gives_no_path_is_refused(self, source_registry, settings):
        app = replace(catalogue.descriptor(RECEIVER), routing_paths={"routing": "config.routing"})
        with pytest.raises(catalogue.CatalogueError, match="destinations"):
            routing.compile_for(app, source_registry, settings)


class TestReceiverOnTheBus:
    def test_a_matched_source_gets_a_rule_and_no_destination(self, source_registry, settings):
        compiled = routing.compile_for(catalogue.descriptor(RECEIVER), source_registry, settings)

        assert compiled["routing"]["source_rules"] == [
            {
                "field": "_json.app",
                "mode": "key_value_set",
                "match_value": "filebeat",
                "source": "filebeat",
            }
        ]
        # The bus holds the record between stages, so nothing is addressed.
        assert compiled["destinations"] == {"default": "kafka", "rules": []}

    def test_a_fetcher_source_is_recognised_by_the_label_its_fetcher_stamps(
        self, fetched_registry, settings
    ):
        compiled = routing.compile_for(catalogue.descriptor(RECEIVER), fetched_registry, settings)
        by_source = {r["source"]: r for r in compiled["routing"]["source_rules"]}

        assert by_source["okta-audit"] == {
            "field": "_source",
            "mode": "key_value_set",
            "match_value": "okta-audit",
            "source": "okta-audit",
        }


class TestReceiverOnDirect:
    def test_every_matched_source_is_addressed(self, direct_settings):
        registry = FakeRegistry([_matched("auth"), _fetched()])

        compiled = routing.compile_for(catalogue.descriptor(RECEIVER), registry, direct_settings)

        assert compiled["destinations"] == {
            "default": "loader",
            "rules": [
                {"match_field": "_json.app", "match_value": "auth", "destination": "loader"},
                {"match_field": "_source", "match_value": "okta-audit", "destination": "loader"},
            ],
        }

    def test_a_transformed_source_is_addressed_to_its_transform_instance(
        self, direct_settings, direct_transforms
    ):
        registry = FakeRegistry([_matched("auth", transform={"engine": "vrl"}), _fetched()])

        compiled = routing.compile_for(catalogue.descriptor(RECEIVER), registry, direct_settings)

        assert compiled["destinations"] == {
            "default": "loader",
            "dfe-transform-vrl-auth": {"grpc": {"endpoint": VRL_AUTH_ENDPOINT}},
            "rules": [
                {
                    "match_field": "_json.app",
                    "match_value": "auth",
                    "destination": "dfe-transform-vrl-auth",
                },
                {"match_field": "_source", "match_value": "okta-audit", "destination": "loader"},
            ],
        }

    def test_a_named_destination_is_addressed_the_way_the_fetcher_addresses_one(
        self, direct_settings, direct_transforms
    ):
        """Both compilers hand the receiver and the fetcher the same gRPC block.

        The receiver deserialises a named destination as ``DestinationSpec``
        (``src/config/mod.rs``), so ``grpc`` is a struct with an ``endpoint``, not
        a URI. Emitting the bare string made receiver v1.15.30 refuse the whole
        config file, and a receiver that will not start takes the direct
        transport with it - which no assertion on the rules would have caught.

        Read on a transform instance, because the loader is the one destination
        the receiver addresses itself.
        """
        registry = FakeRegistry([_fetched(transform={"engine": "vrl"})])

        receiver = routing.compile_for(catalogue.descriptor(RECEIVER), registry, direct_settings)
        fetcher = routing.compile_for(
            catalogue.descriptor(FETCHER), registry, direct_settings, instance="okta-audit"
        )

        named = receiver["destinations"]["dfe-transform-vrl-okta-audit"]
        assert named["grpc"] == fetcher["output"]["grpc"]

    def test_the_loader_destination_is_named_but_never_addressed(self, direct_settings):
        """#436: an endpoint here wins over the address the loader is deployed on.

        The receiver synthesises its built-in ``loader`` destination from
        ``loader.grpc_endpoint``, and only while the destination set has not
        declared one (``src/config/mod.rs`` ``resolved_destinations``). Rendering
        an endpoint here short-circuits that, which sent every record on the
        no-bus tier to a port the loader does not listen on.
        """
        registry = FakeRegistry([_matched("auth"), _fetched()])

        compiled = routing.compile_for(catalogue.descriptor(RECEIVER), registry, direct_settings)

        assert "loader" not in compiled["destinations"]
        assert compiled["destinations"]["default"] == "loader"
        assert [r["destination"] for r in compiled["destinations"]["rules"]] == [
            "loader",
            "loader",
        ]

    def test_the_labelling_rule_and_the_destination_rule_read_one_match(self, direct_settings):
        registry = FakeRegistry([_matched("auth")])

        compiled = routing.compile_for(catalogue.descriptor(RECEIVER), registry, direct_settings)

        rule = compiled["routing"]["source_rules"][0]
        destination = compiled["destinations"]["rules"][0]
        assert (rule["field"], rule["match_value"]) == (
            destination["match_field"],
            destination["match_value"],
        )

    def test_the_default_flow_compiles_to_the_default_destination(
        self, direct_settings, direct_transforms
    ):
        # `always` matches everything, so it takes the destination an unmatched
        # record already goes to rather than a rule that would shadow the rest.
        default_flow = _matched("default", transform={"engine": "vrl"})
        default_flow.versions["1.0.0"].match.operator = "always"
        registry = FakeRegistry([default_flow])

        compiled = routing.compile_for(catalogue.descriptor(RECEIVER), registry, direct_settings)

        assert compiled["destinations"]["default"] == "dfe-transform-vrl-default"
        assert compiled["destinations"]["rules"] == []
        assert compiled["routing"]["source_rules"] == []


class TestArchivedOnDirect:
    """Nothing holds the record, so the rule that sends it must name both stages."""

    def test_an_archived_source_reaches_the_archiver_beside_the_loader(self, direct_settings):
        registry = FakeRegistry([_matched("auth", archive=True)])

        compiled = routing.compile_for(catalogue.descriptor(RECEIVER), registry, direct_settings)

        assert compiled["destinations"] == {
            "default": "loader",
            "dfe-archiver": {"grpc": {"endpoint": ARCHIVER_ENDPOINT, "confirms_delivery": False}},
            "rules": [
                {
                    "match_field": "_json.app",
                    "match_value": "auth",
                    "destination": ["loader", ARCHIVER],
                }
            ],
        }

    def test_only_the_archiver_leg_is_declared_as_not_confirming_delivery(
        self, direct_settings, direct_transforms
    ):
        # The archiver answers a push once the record is queued, so the receiver
        # must not report that leg at least once. The loader and a transform confirm.
        registry = FakeRegistry(
            [_matched("auth", archive=True, transform={"engine": "vrl"}), _matched("syslog")]
        )

        destinations = routing.compile_for(
            catalogue.descriptor(RECEIVER), registry, direct_settings
        )["destinations"]

        assert destinations[ARCHIVER]["grpc"]["confirms_delivery"] is False
        assert destinations["dfe-transform-vrl-auth"] == {"grpc": {"endpoint": VRL_AUTH_ENDPOINT}}
        assert "loader" not in destinations

    def test_the_raw_copy_is_taken_before_the_transform_not_after(
        self, direct_settings, direct_transforms
    ):
        # The archiver sits beside the transform, not behind it: archive keeps the
        # record as it ARRIVED, so a transformed source must still name both.
        registry = FakeRegistry([_matched("auth", archive=True, transform={"engine": "vrl"})])

        compiled = routing.compile_for(catalogue.descriptor(RECEIVER), registry, direct_settings)

        assert compiled["destinations"]["rules"][0]["destination"] == [
            "dfe-transform-vrl-auth",
            ARCHIVER,
        ]

    def test_a_source_that_asked_for_none_names_one_destination(self, direct_settings):
        registry = FakeRegistry([_matched("auth")])

        compiled = routing.compile_for(catalogue.descriptor(RECEIVER), registry, direct_settings)

        assert compiled["destinations"]["rules"][0]["destination"] == "loader"
        assert ARCHIVER not in compiled["destinations"]

    def test_the_default_flow_fans_out_through_the_default(self, direct_settings):
        # `always` compiles to the default rather than a rule, so an archived main
        # flow has to reach the archiver there or not at all.
        main = _matched("main", archive=True)
        main.versions["1.0.0"].match.operator = "always"
        registry = FakeRegistry([main])

        compiled = routing.compile_for(catalogue.descriptor(RECEIVER), registry, direct_settings)

        assert compiled["destinations"]["default"] == ["loader", ARCHIVER]
        assert compiled["destinations"][ARCHIVER] == {
            "grpc": {"endpoint": ARCHIVER_ENDPOINT, "confirms_delivery": False}
        }


class TestAStoredArchiveNothingCopies:
    """Fetched and archived on direct: refused at save, compiled exactly as stored."""

    def test_its_fetcher_routing_is_what_it_is_without_the_archive(self, direct_settings):
        fetcher = catalogue.descriptor(FETCHER)

        plain = routing.compile_for(
            fetcher, FakeRegistry([_fetched()]), direct_settings, instance="okta-audit"
        )
        archived = routing.compile_for(
            fetcher, FakeRegistry([_fetched(archive=True)]), direct_settings, instance="okta-audit"
        )

        assert archived == plain
        assert archived["output"] == {"type": "grpc", "grpc": {"endpoint": LOADER_ENDPOINT}}

    def test_the_receiver_compile_names_it_and_keeps_its_rule(self, direct_settings, monkeypatch):
        from dfe_engine.services import source_routing

        warnings: list[str] = []
        monkeypatch.setattr(
            source_routing.logger, "warning", lambda message, **fields: warnings.append(message)
        )

        compiled = routing.compile_for(
            catalogue.descriptor(RECEIVER), FakeRegistry([_fetched(archive=True)]), direct_settings
        )

        assert [w for w in warnings if "'okta-audit'" in w and "archived on the bus" in w]
        assert compiled["destinations"]["rules"] == [
            {
                "match_field": "_source",
                "match_value": "okta-audit",
                "destination": ["loader", ARCHIVER],
            }
        ]


class TestArchiverStack:
    def test_it_reads_the_landing_topic_of_every_source_that_asked(self, settings):
        registry = FakeRegistry([_matched("auth", archive=True), _matched("filebeat")])

        compiled = routing.compile_for(catalogue.descriptor(ARCHIVER), registry, settings)

        assert compiled == {"topics": ["auth_land"]}

    def test_a_deployment_where_nobody_asked_archives_nothing(self, settings):
        # An empty list rather than no block: the archiver's own idle_when reads
        # this path, so the deployment says "no work" instead of falling back to
        # whatever pattern it started with.
        compiled = routing.compile_for(
            catalogue.descriptor(ARCHIVER), FakeRegistry([_matched("filebeat")]), settings
        )

        assert compiled == {"topics": []}

    def test_a_disabled_source_is_not_archived(self, settings):
        registry = FakeRegistry([_matched("auth", archive=True, state="disabled")])

        assert routing.compile_for(catalogue.descriptor(ARCHIVER), registry, settings) == {
            "topics": []
        }

    def test_on_direct_it_reads_no_topic_at_all(self, direct_settings):
        # There is no landing topic to read: the receiver is handed the archiver
        # as a second destination instead.
        registry = FakeRegistry([_matched("auth", archive=True)])

        assert routing.compile_for(catalogue.descriptor(ARCHIVER), registry, direct_settings) == {
            "topics": []
        }

    def test_the_topic_list_lands_where_the_archiver_reads_it(self, settings):
        doc = instances.initial_overlay(instances.instance_of(ARCHIVER, "default"))
        registry = FakeRegistry([_matched("auth", archive=True)])

        routing.sync(catalogue.descriptor(ARCHIVER), doc, registry, settings)

        assert doc["config"]["kafka"]["topics"] == ["auth_land"]

    def test_a_hand_edited_topic_list_is_drift(self, settings):
        # A pattern or an extra topic typed in by hand archives what no source
        # asked for, which is the drift this block exists to remove.
        doc = instances.initial_overlay(instances.instance_of(ARCHIVER, "default"))
        registry = FakeRegistry([_matched("auth", archive=True)])
        app = catalogue.descriptor(ARCHIVER)
        routing.sync(app, doc, registry, settings)

        doc["config"]["kafka"]["topics"] = ["auth_land", "filebeat_land"]

        assert routing.status(app, doc, registry, settings).drift is True
        assert routing.sync(app, doc, registry, settings) is True
        assert doc["config"]["kafka"]["topics"] == ["auth_land"]


class TestReceiverStack:
    def test_the_block_carries_only_the_keys_the_sources_derive(self, source_registry, settings):
        """#401: the deployment's own dead-letter settings must survive a sync.

        ``ReceiverRoutingConfig.dlq`` and ``legacy_compat`` are model defaults the
        compile never sets, and the block is written whole, so emitting them
        stamps the deployment's own values back to the default every time.
        """
        compiled = routing.compile_for(catalogue.descriptor(RECEIVER), source_registry, settings)

        assert "dlq" not in compiled["routing"]
        assert "legacy_compat" not in compiled["routing"]


class TestLoaderStack:
    def test_the_block_carries_only_the_keys_the_sources_derive(self, source_registry, settings):
        """A model default here would land on a key the deployment set itself.

        The block is written whole, and the loader's own struct defaults every
        field, so a key nobody derived belongs to the deployment - its
        dead-letter switch, its topic suffixes - and must not appear here.
        """
        compiled = routing.compile_for(catalogue.descriptor(LOADER), source_registry, settings)

        assert compiled == {
            "routing": {
                "default_db": settings.clickhouse.effective_data_database,
                "source_to_table": {"filebeat": "filebeat"},
            }
        }


class TestLoaderCapture:
    """Stage 3: a derived schema's capture switches reach the loader's config."""

    @staticmethod
    def _deployment(tmp_path, *, capture_json: bool, capture_raw: bool):
        from dfe_engine.schema.derived import DerivedSchema
        from dfe_engine.schema.derived_registry import DerivedSchemaRegistry, derived_reference
        from dfe_engine.settings import DFESettings

        settings = DFESettings(
            env="dev", transport={"default": "bus"}, schemas={"schemas_dir": str(tmp_path)}
        )
        store = DerivedSchemaRegistry.from_settings(settings)
        store.save(
            DerivedSchema.model_validate(
                {
                    "base": "meta/beats/filebeat",
                    "base_version": "1.0.0",
                    "current": "1.0.0",
                    "path": derived_reference("beats/filebeat_auth"),
                    "versions": {
                        "1.0.0": {
                            "date": "2026-09-21",
                            "summary": "system.auth subset",
                            "capture_json": capture_json,
                            "capture_raw": capture_raw,
                            "select": [{"name": "timestamp"}],
                        }
                    },
                }
            )
        )
        registry = FakeRegistry(
            [
                _matched(
                    "filebeat",
                    schema_config={"derived_schema": "derived/beats/filebeat_auth"},
                )
            ]
        )
        return settings, registry

    def test_turning_the_catch_all_off_reaches_the_loader(self, tmp_path):
        settings, registry = self._deployment(tmp_path, capture_json=False, capture_raw=False)

        compiled = routing.compile_for(catalogue.descriptor(LOADER), registry, settings)

        db = settings.clickhouse.effective_data_database
        assert compiled["capture"] == {f"{db}.filebeat": "extracted_only"}

    def test_dropping_json_alone_leaves_raw_populated(self, tmp_path):
        settings, registry = self._deployment(tmp_path, capture_json=False, capture_raw=True)

        compiled = routing.compile_for(catalogue.descriptor(LOADER), registry, settings)

        db = settings.clickhouse.effective_data_database
        assert compiled["capture"] == {f"{db}.filebeat": "raw_only"}

    def test_keeping_json_alone_compiles_to_json_only(self, tmp_path):
        from dfe_engine.services.models.loader import LoaderConfig

        settings, registry = self._deployment(tmp_path, capture_json=True, capture_raw=False)
        app = catalogue.descriptor(LOADER)
        doc = instances.initial_overlay(instances.instance_of(LOADER, "default"))

        assert routing.sync(app, doc, registry, settings) is True

        db = settings.clickhouse.effective_data_database
        config = LoaderConfig.model_validate(doc["config"])
        assert config.metadata.table_capture_modes == {f"{db}.filebeat": "json_only"}

    def test_the_safety_net_left_on_writes_nothing(self, tmp_path):
        settings, registry = self._deployment(tmp_path, capture_json=True, capture_raw=True)

        compiled = routing.compile_for(catalogue.descriptor(LOADER), registry, settings)

        assert "capture" not in compiled

    def test_the_compiled_value_lands_where_the_loader_reads_it(self, tmp_path):
        # The engine authors both ends of this connection, so assert that what it
        # writes into the overlay is a config the loader model accepts, on the
        # key the loader's router builds (<db>.<table>).
        from dfe_engine.services.models.loader import LoaderConfig

        settings, registry = self._deployment(tmp_path, capture_json=False, capture_raw=False)
        app = catalogue.descriptor(LOADER)
        doc = instances.initial_overlay(instances.instance_of(LOADER, "default"))

        assert routing.sync(app, doc, registry, settings) is True

        db = settings.clickhouse.effective_data_database
        config = LoaderConfig.model_validate(doc["config"])
        assert config.metadata.table_capture_modes == {f"{db}.filebeat": "extracted_only"}

    def test_a_manifest_with_no_capture_path_says_so_rather_than_failing(
        self, tmp_path, monkeypatch
    ):
        # dfe-infra's manifest is the SSoT and a deployment may mount an older
        # copy of it; the compile reflects what that copy declares.
        warnings: list[str] = []
        monkeypatch.setattr(routing.logger, "warning", warnings.append)
        settings, registry = self._deployment(tmp_path, capture_json=False, capture_raw=False)
        app = replace(catalogue.descriptor(LOADER), routing_paths={"routing": "config.routing"})

        compiled = routing.compile_for(app, registry, settings)

        assert "capture" not in compiled
        assert "keep populating _json and _raw" in warnings[0]

    def test_turning_it_back_on_clears_the_overlay_entry(self, tmp_path):
        app = catalogue.descriptor(LOADER)
        doc = instances.initial_overlay(instances.instance_of(LOADER, "default"))
        off_settings, off_registry = self._deployment(
            tmp_path / "off", capture_json=False, capture_raw=False
        )
        routing.sync(app, doc, off_registry, off_settings)

        on_settings, on_registry = self._deployment(
            tmp_path / "on", capture_json=True, capture_raw=True
        )
        assert routing.sync(app, doc, on_registry, on_settings) is True
        assert "table_capture_modes" not in doc["config"].get("metadata", {})


class TestFetcherInstance:
    def test_on_the_bus_it_lands_on_its_own_topic(self, fetched_registry, settings):
        compiled = routing.compile_for(
            catalogue.descriptor(FETCHER), fetched_registry, settings, instance="okta-audit"
        )

        assert compiled == {
            "sources": {
                "okta": {
                    "enabled": True,
                    "topic": "okta-audit",
                    "tenant_url": "https://example.okta.com",
                }
            },
            "output": {"type": "kafka"},
        }

    def test_on_direct_it_pushes_at_the_loader(self, direct_settings):
        compiled = routing.compile_for(
            catalogue.descriptor(FETCHER),
            FakeRegistry([_fetched()]),
            direct_settings,
            instance="okta-audit",
        )

        assert compiled["output"] == {
            "type": "grpc",
            "grpc": {"endpoint": LOADER_ENDPOINT},
        }

    def test_on_direct_a_transformed_source_pushes_at_its_transform(
        self, direct_settings, direct_transforms
    ):
        registry = FakeRegistry([_fetched(transform={"engine": "vrl"})])

        compiled = routing.compile_for(
            catalogue.descriptor(FETCHER), registry, direct_settings, instance="okta-audit"
        )

        assert compiled["output"]["grpc"] == {
            "endpoint": "http://dfe-transform-vrl-okta-audit:6000"
        }

    def test_two_connector_types_in_one_fetcher_are_refused(self, settings):
        """The composer is the second gate: a stored source that spans two is not deployed."""
        registry = FakeRegistry(
            [
                _fetched(
                    fetcher={
                        "config": {
                            "connections": [
                                {"id": "tenant-a"},
                                {"id": "falcon", "type": "crowdstrike"},
                            ]
                        }
                    }
                )
            ]
        )

        with pytest.raises(MixedConnectorTypesError, match="crowdstrike, okta") as refusal:
            routing.compile_for(
                catalogue.descriptor(FETCHER), registry, settings, instance="okta-audit"
            )
        assert "'okta-audit'" in str(refusal.value)

    def test_one_type_across_several_connections_compiles(self, settings):
        """Many accounts, one type, one stanza - the shape the suite supports."""
        connections = [{"id": "tenant-a"}, {"id": "tenant-b"}, {"id": "tenant-c"}]
        registry = FakeRegistry([_fetched(fetcher={"config": {"connections": connections}})])

        compiled = routing.compile_for(
            catalogue.descriptor(FETCHER), registry, settings, instance="okta-audit"
        )

        assert compiled["sources"] == {
            "okta": {"enabled": True, "topic": "okta-audit", "connections": connections}
        }

    def test_the_override_dial_compiles_a_mixed_fetcher_with_a_warning(self, monkeypatch):
        from dfe_engine.settings import DFESettings
        from dfe_engine.source import alignment

        warnings: list[dict] = []
        monkeypatch.setattr(
            alignment.logger, "warning", lambda message, **fields: warnings.append(fields)
        )
        registry = FakeRegistry(
            [
                _fetched(
                    fetcher={"config": {"connections": [{"id": "falcon", "type": "crowdstrike"}]}}
                )
            ]
        )

        compiled = routing.compile_for(
            catalogue.descriptor(FETCHER),
            registry,
            DFESettings(env="dev", source={"allow_mixed_fetcher_types": True}),
            instance="okta-audit",
        )

        assert set(compiled["sources"]) == {"okta"}
        assert warnings == [
            {
                "source": "okta-audit",
                "types": ["crowdstrike", "okta"],
                "setting": alignment.MIXED_TYPES_SETTING,
            }
        ]

    def test_a_route_sends_matched_records_to_another_sources_landing(self, settings):
        registry = FakeRegistry(
            [
                _fetched(
                    fetcher={
                        "routes": [
                            {
                                "match": {"field": "event.kind", "value": "alert"},
                                "source": "okta-alerts",
                            }
                        ]
                    }
                ),
                _matched("okta-alerts"),
            ]
        )

        compiled = routing.compile_for(
            catalogue.descriptor(FETCHER), registry, settings, instance="okta-audit"
        )

        assert compiled["output"]["destinations"] == {
            "okta-alerts": {"kafka": {"topic": "okta-alerts"}}
        }
        assert compiled["output"]["routes"] == [
            {"match_field": "event.kind", "match_value": "alert", "destination": "okta-alerts"}
        ]

    def test_a_route_on_direct_carries_an_endpoint(self, direct_settings, direct_transforms):
        registry = FakeRegistry(
            [
                _fetched(
                    fetcher={
                        "routes": [
                            {
                                "match": {"field": "event.kind", "value": "alert"},
                                "source": "okta-alerts",
                            }
                        ]
                    }
                ),
                _matched("okta-alerts", transform={"engine": "vrl"}),
            ]
        )

        compiled = routing.compile_for(
            catalogue.descriptor(FETCHER), registry, direct_settings, instance="okta-audit"
        )

        assert compiled["output"]["destinations"] == {
            "okta-alerts": {"grpc": {"endpoint": "http://dfe-transform-vrl-okta-alerts:6000"}}
        }
        assert compiled["output"]["routes"] == [
            {"match_field": "event.kind", "match_value": "alert", "destination": "okta-alerts"}
        ]

    @pytest.mark.parametrize(
        ("target", "reason"),
        [(None, "not defined"), ("dormant", "dormant")],
    )
    def test_a_route_to_a_source_that_cannot_take_records_is_refused(
        self, settings, target, reason
    ):
        sources = [
            _fetched(
                fetcher={
                    "routes": [
                        {"match": {"field": "event.kind", "value": "alert"}, "source": "gone"}
                    ]
                }
            )
        ]
        if target is not None:
            sources.append(_matched("gone", state=target))

        with pytest.raises(routing.RoutingNotApplicableError, match=reason):
            routing.compile_for(
                catalogue.descriptor(FETCHER),
                FakeRegistry(sources),
                settings,
                instance="okta-audit",
            )

    def test_a_receiver_source_has_no_fetcher_block(self, fetched_registry, settings):
        with pytest.raises(routing.RoutingNotApplicableError, match="receiver-based"):
            routing.compile_for(
                catalogue.descriptor(FETCHER), fetched_registry, settings, instance="filebeat"
            )

    def test_an_unknown_source_has_no_fetcher_block(self, fetched_registry, settings):
        with pytest.raises(routing.RoutingNotApplicableError, match="no source"):
            routing.compile_for(
                catalogue.descriptor(FETCHER), fetched_registry, settings, instance="nonesuch"
            )

    def test_a_stack_scoped_app_ignores_the_instance(self, fetched_registry, settings):
        app = catalogue.descriptor(RECEIVER)
        assert routing.compile_for(
            app, fetched_registry, settings, instance="okta-audit"
        ) == routing.compile_for(app, fetched_registry, settings)

    def test_sync_writes_the_stanza_into_the_instance_overlay(self, fetched_registry, settings):
        app = instances.instance_of(FETCHER, "okta-audit")
        doc = instances.initial_overlay(app)
        assert routing.sync(app.descriptor, doc, fetched_registry, settings, instance="okta-audit")
        assert doc["config"]["sources"]["okta"]["topic"] == "okta-audit"
        assert doc["config"]["instance_id"] == "okta-audit"

    def test_instances_needing_sync_skips_an_instance_with_no_source(
        self, fetched_registry, settings
    ):
        orphan = instances.instance_of(FETCHER, "gone")
        live = instances.instance_of(FETCHER, "okta-audit")
        found = routing.instances_needing_sync(
            [(orphan, instances.initial_overlay(orphan)), (live, instances.initial_overlay(live))],
            fetched_registry,
            settings,
        )
        assert [app.instance for app, _ in found] == ["okta-audit"]


class TestTransformInstance:
    def test_on_the_bus_it_reads_land_and_writes_load(self, settings):
        registry = FakeRegistry([_matched("auth", transform={"engine": "vrl"})])

        compiled = routing.compile_for(
            catalogue.descriptor(VRL), registry, settings, instance="auth"
        )

        assert compiled == {
            "source": {"topics": ["auth_land"], "group_id": "dfe-transform-vrl-auth"},
            "sink": {"topic": "auth_load"},
        }

    def test_on_direct_it_listens_and_pushes_at_the_loader_under_its_load_key(
        self, direct_settings, direct_transforms
    ):
        # The loader takes a record's table from its key when the program left no
        # _source in it, so an empty key lands the source in the main table.
        registry = FakeRegistry([_matched("auth", transform={"engine": "vrl"})])

        compiled = routing.compile_for(
            catalogue.descriptor(VRL), registry, direct_settings, instance="auth"
        )

        assert compiled == {
            "source": {"transport": "grpc", "listen": "0.0.0.0:6000"},
            "sink": {"transport": "grpc", "endpoint": LOADER_ENDPOINT, "topic": "auth_load"},
        }

    def test_the_output_key_is_the_same_on_both_transports(
        self, settings, direct_settings, direct_transforms
    ):
        registry = FakeRegistry([_matched("auth", transform={"engine": "vrl"})])
        app = catalogue.descriptor(VRL)

        bus = routing.compile_for(app, registry, settings, instance="auth")
        direct = routing.compile_for(app, registry, direct_settings, instance="auth")

        assert direct["sink"]["topic"] == bus["sink"]["topic"] == "auth_load"

    def test_on_direct_vector_keeps_the_name_it_derives_its_output_key_from(self, direct_settings):
        # dfe-transform-vector derives sink.topic from dfe_source on either
        # transport, and its bridge stamps that topic on every record it pushes.
        registry = FakeRegistry([_matched("auth", transform={"engine": "vector"})])

        compiled = routing.compile_for(
            catalogue.descriptor("dfe-transform-vector"), registry, direct_settings, instance="auth"
        )

        assert compiled == {
            "dfe_source": "auth",
            "source": {"transport": "grpc", "listen": "0.0.0.0:6000"},
            "sink": {"transport": "grpc", "endpoint": LOADER_ENDPOINT},
        }

    def test_moving_a_source_to_direct_clears_its_bus_wiring(
        self, settings, direct_settings, direct_transforms
    ):
        # The overlay is seeded with the bus binding at deploy, so the listener
        # has to replace the source block whole rather than sit beside its topics.
        registry = FakeRegistry([_matched("auth", transform={"engine": "vrl"})])
        app = instances.instance_of(VRL, "auth")
        doc = instances.initial_overlay(app)
        routing.sync(app.descriptor, doc, registry, settings, instance="auth")
        assert doc["config"]["source"]["topics"] == ["auth_land"]

        routing.sync(app.descriptor, doc, registry, direct_settings, instance="auth")

        assert doc["config"]["source"] == {"transport": "grpc", "listen": "0.0.0.0:6000"}
        assert doc["config"]["sink"] == {
            "transport": "grpc",
            "endpoint": LOADER_ENDPOINT,
            "topic": "auth_load",
        }

    def test_an_overlay_synced_before_the_load_key_is_drift(
        self, direct_settings, direct_transforms
    ):
        # A direct instance deployed without the key has to be re-synced, not left
        # sending every record the program strips of _source to the main table.
        registry = FakeRegistry([_matched("auth", transform={"engine": "vrl"})])
        app = instances.instance_of(VRL, "auth")
        doc = instances.initial_overlay(app)
        routing.sync(app.descriptor, doc, registry, direct_settings, instance="auth")
        del doc["config"]["sink"]["topic"]

        found = routing.status(app.descriptor, doc, registry, direct_settings, instance="auth")

        assert found.drift
        assert routing.sync(app.descriptor, doc, registry, direct_settings, instance="auth")
        assert doc["config"]["sink"]["topic"] == "auth_load"

    def test_the_variant_lands_where_the_app_names_it(self, settings):
        elastic = catalogue.descriptor("dfe-transform-elastic")
        registry = FakeRegistry(
            [_matched("auth", transform={"engine": "elastic", "variant": "okta_system"})]
        )

        compiled = routing.compile_for(elastic, registry, settings, instance="auth")

        assert compiled["source"]["name"] == "okta_system"

    def test_a_source_with_no_transform_has_no_instance(self, settings):
        with pytest.raises(routing.RoutingNotApplicableError, match="no transform"):
            routing.compile_for(
                catalogue.descriptor(VRL),
                FakeRegistry([_matched("auth")]),
                settings,
                instance="auth",
            )

    def test_a_source_transformed_by_another_app_has_no_instance_here(self, settings):
        registry = FakeRegistry([_matched("auth", transform={"engine": "vector"})])

        with pytest.raises(routing.RoutingNotApplicableError, match="dfe-transform-vector"):
            routing.compile_for(catalogue.descriptor(VRL), registry, settings, instance="auth")
