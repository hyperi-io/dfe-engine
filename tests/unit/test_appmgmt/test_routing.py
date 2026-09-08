#  Project:      dfe-engine
#  File:         tests/unit/test_appmgmt/test_routing.py
#  Purpose:      Tests for delivering source-derived routing into an overlay
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Routing is derived, so the overlay is compared to the sources, never trusted.

The regression these guard is the one found on devex: a deployed receiver whose
config was an empty document, running on built-in defaults while every source
rule ever defined was ignored.
"""

from __future__ import annotations

import pytest

from dfe_engine.appmgmt import catalogue, instances, routing
from dfe_engine.source.models import Source
from dfe_engine.source.registry import SourceNotFoundError

RECEIVER = "dfe-receiver"
LOADER = "dfe-loader"
VRL = "dfe-transform-vrl"


class _Settings:
    """The one settings field the loader compiler reads."""

    class clickhouse:  # noqa: N801 - mirrors the settings attribute path
        effective_data_database = "dfe"


class _Registry:
    """The two reader methods the routing compilers call."""

    def __init__(self, sources: list[Source]) -> None:
        self._sources = {s.source: s for s in sources}

    def get_source(self, source_name: str) -> Source:
        if source_name not in self._sources:
            raise SourceNotFoundError(f"Source {source_name!r} not found")
        return self._sources[source_name]

    def get_all_sources(
        self, enabled_only: bool = False, *, states: tuple[str, ...] | None = None
    ) -> list[Source]:
        sources = list(self._sources.values())
        if states is not None:
            return [s for s in sources if s.state in states]
        if enabled_only:
            return [s for s in sources if s.enabled]
        return sources


@pytest.fixture
def settings():
    return _Settings()


@pytest.fixture
def source_registry():
    """One filebeat source routed by a JSON discriminator, as WS21 uses."""
    return _Registry(
        [
            Source.model_validate(
                {
                    "source": "filebeat",
                    "state": "active",
                    "match": {"field": "_source", "operator": "equals", "value": "filebeat"},
                }
            )
        ]
    )


@pytest.fixture
def overlay():
    return instances.initial_overlay(instances.instance_of(RECEIVER, "default"))


class TestManifest:
    def test_the_receiver_declares_a_compiler_and_a_path(self):
        app = catalogue.descriptor(RECEIVER)
        assert app.has_compiled_routing is True
        assert app.routing_compiler == "receiver"
        assert app.routing_path == "config.routing"

    def test_the_loader_declares_its_own(self):
        app = catalogue.descriptor(LOADER)
        assert (app.routing_compiler, app.routing_path) == ("loader", "config.routing")

    def test_an_app_without_derived_routing_says_so(self):
        assert catalogue.descriptor(VRL).has_compiled_routing is False

    def test_every_declared_compiler_is_implemented(self):
        # A manifest naming a compiler the engine does not have would fail at
        # request time on a deployed cluster, which is the worst place to find it.
        declared = {
            app.routing_compiler for app in catalogue.APP_CATALOGUE.values() if app.routing_compiler
        }
        assert declared <= set(routing.compilers())


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

    def test_syncing_twice_is_not_a_change(self, overlay, source_registry, settings):
        app = catalogue.descriptor(RECEIVER)
        assert routing.sync(app, overlay, source_registry, settings) is True
        assert routing.sync(app, overlay, source_registry, settings) is False

    def test_an_app_without_derived_routing_refuses(self, source_registry, settings):
        with pytest.raises(routing.RoutingNotCompiledError):
            routing.compile_for(catalogue.descriptor(VRL), source_registry, settings)


class TestCompiled:
    def test_the_receiver_block_carries_source_rules(self, overlay, source_registry, settings):
        app = catalogue.descriptor(RECEIVER)
        compiled = routing.compile_for(app, source_registry, settings)
        assert "source_rules" in compiled

    def test_the_loader_block_is_compiled_for_the_configured_database(
        self, source_registry, settings
    ):
        app = catalogue.descriptor(LOADER)
        compiled = routing.compile_for(app, source_registry, settings)
        assert isinstance(compiled, dict)

    def test_an_unknown_compiler_raises_rather_than_returning_nothing(
        self, source_registry, settings
    ):
        from dataclasses import replace

        app = replace(catalogue.descriptor(RECEIVER), routing_compiler="nonexistent")
        with pytest.raises(routing.UnknownRoutingCompilerError):
            routing.compile_for(app, source_registry, settings)


FETCHER = "dfe-fetcher"


@pytest.fixture
def fetched_registry():
    return _Registry(
        [
            Source.model_validate(
                {
                    "source": "okta-audit",
                    "state": "active",
                    "fetcher": {
                        "source_type": "okta",
                        "config": {
                            "tenant_url": "https://example.okta.com",
                            "credential_secret": "vault:secret/okta:token",
                            "services": [{"name": "system_log"}],
                        },
                    },
                }
            ),
            Source.model_validate(
                {
                    "source": "filebeat",
                    "state": "active",
                    "match": {"field": "_source", "operator": "equals", "value": "filebeat"},
                }
            ),
        ]
    )


class TestInstanceScope:
    def test_the_fetcher_is_instance_scoped(self):
        app = catalogue.descriptor(FETCHER)
        assert app.routing_is_per_instance is True
        assert app.routing_path == "config.sources"
        assert catalogue.descriptor(RECEIVER).routing_is_per_instance is False

    def test_the_block_is_the_bound_source_stanza(self, fetched_registry, settings):
        compiled = routing.compile_for(
            catalogue.descriptor(FETCHER), fetched_registry, settings, instance="okta-audit"
        )
        assert compiled == {
            "okta": {
                "enabled": True,
                "topic": "okta-audit",
                "tenant_url": "https://example.okta.com",
                "credential_secret": "vault:secret/okta:token",
                "services": [{"name": "system_log"}],
            }
        }

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

    def test_the_receiver_carries_the_fetcher_rule(self, fetched_registry, settings):
        compiled = routing.compile_for(catalogue.descriptor(RECEIVER), fetched_registry, settings)
        by_source = {r["source"]: r for r in compiled["source_rules"]}
        assert by_source["okta-audit"] == {
            "field": "_source",
            "mode": "key_value_set",
            "match_value": "okta-audit",
            "source": "okta-audit",
        }
        assert by_source["filebeat"]["mode"] == "key_value_set"
