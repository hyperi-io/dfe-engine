#  Project:      dfe-engine
#  File:         tests/unit/test_appmgmt/test_derived.py
#  Purpose:      Tests for keeping the deploy repo's derived app state in step
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Planning the deploy-repo writes the source definitions imply."""

from __future__ import annotations

import pytest

from dfe_engine.appmgmt import derived, instances
from dfe_engine.gitcrud.engine import get_path
from dfe_engine.source.models import Source, SourceWriteRequest, source_from_write

from .conftest import FakeRegistry as _Registry

RECEIVER = "dfe-receiver"
LOADER = "dfe-loader"
FETCHER = "dfe-fetcher"
VRL = "dfe-transform-vrl"
ACTOR = "test"


def _receiver_source(name: str = "filebeat", state: str = "active", **fields) -> Source:
    return Source.model_validate(
        {
            "source": name,
            "state": state,
            "deployed_version": "1.0.0",
            "match": {"field": "_json.app", "operator": "equals", "value": name},
            **fields,
        }
    )


def _fetcher_source(
    name: str = "crates-audit",
    *,
    state: str = "active",
    deployed: bool = True,
    topic: str = "own",
) -> Source:
    doc = {
        "source": name,
        "state": state,
        "fetcher": {
            "source_type": "crates_io",
            "topic": topic,
            "config": {"crates": ["dfe-fetcher"], "interval_secs": 3600},
        },
    }
    if deployed:
        doc["deployed_version"] = "1.0.0"
    return Source.model_validate(doc)


def _put(crud, app, doc):
    crud.put(instances.HELMVARS_CLASS, app.overlay_name, doc, ACTOR, message="test: put")


def _apply(crud, changes):
    """Apply a plan the way the API does, so a second plan can be checked."""
    for change in changes:
        if change.action == "remove":
            crud.delete(instances.HELMVARS_CLASS, change.app.overlay_name, ACTOR, message="rm")
        else:
            _put(crud, change.app, change.doc)


class TestReconcile:
    def test_a_fresh_deploy_gets_its_routing_compiled_in(self, crud, direct_settings):
        for service in (RECEIVER, LOADER):
            app = instances.instance_of(service, "default")
            _put(crud, app, instances.initial_overlay(app))

        done = derived.reconcile(crud, _Registry([]), direct_settings)

        assert set(done) == {
            f"{RECEIVER}/default: sync routing",
            f"{LOADER}/default: sync routing",
        }
        receiver = instances.instance_of(RECEIVER, "default")
        doc = instances.read_overlay(crud, receiver)
        assert get_path(doc, "config.routing.default_source") == "main"
        # The loader is referenced and left unaddressed: the receiver resolves it
        # from its own loader.grpc_endpoint.
        assert get_path(doc, "config.destinations.default") == "loader"
        assert get_path(doc, "config.destinations.loader") is None
        assert instances.history(crud, receiver)[0].actor == derived.ENGINE_ACTOR

    def test_a_reconciled_repo_is_left_alone(self, crud, settings):
        app = instances.instance_of(RECEIVER, "default")
        _put(crud, app, instances.initial_overlay(app))
        registry = _Registry([_receiver_source()])
        derived.reconcile(crud, registry, settings)

        assert derived.reconcile(crud, registry, settings) == []


class TestStackRouting:
    def test_a_deployed_receiver_with_no_routing_is_synced(self, crud, settings):
        app = instances.instance_of(RECEIVER, "main")
        _put(crud, app, instances.initial_overlay(app))

        changes = derived.plan(crud, _Registry([_receiver_source()]), settings)

        assert [(c.app.service, c.action) for c in changes] == [(RECEIVER, "sync")]
        rules = get_path(changes[0].doc, "config.routing.source_rules")
        assert [r["source"] for r in rules] == ["filebeat"]

    def test_a_synced_overlay_is_left_alone(self, crud, settings):
        app = instances.instance_of(RECEIVER, "main")
        _put(crud, app, instances.initial_overlay(app))
        registry = _Registry([_receiver_source()])
        _apply(crud, derived.plan(crud, registry, settings))

        assert derived.plan(crud, registry, settings) == []

    def test_a_new_source_changes_the_receiver_and_the_loader(self, crud, settings):
        for service in (RECEIVER, LOADER):
            app = instances.instance_of(service, "main")
            _put(crud, app, instances.initial_overlay(app))
        registry = _Registry([_receiver_source()])
        _apply(crud, derived.plan(crud, registry, settings))

        changes = derived.plan(
            crud, _Registry([_receiver_source(), _receiver_source("syslog")]), settings
        )

        assert {c.app.service for c in changes} == {RECEIVER, LOADER}
        assert all(c.action == "sync" for c in changes)

    def test_a_fetcher_source_adds_its_receiver_rule(self, crud, settings):
        app = instances.instance_of(RECEIVER, "main")
        _put(crud, app, instances.initial_overlay(app))

        changes = derived.plan(crud, _Registry([_fetcher_source()]), settings)

        receiver = next(c for c in changes if c.app.service == RECEIVER)
        rules = get_path(receiver.doc, "config.routing.source_rules")
        assert rules == [
            {
                "field": "_source",
                "mode": "key_value_set",
                "match_value": "crates-audit",
                "source": "crates-audit",
            }
        ]


class TestFetcherInstances:
    def test_a_deployed_fetcher_source_gets_an_instance(self, crud, settings):
        changes = derived.plan(crud, _Registry([_fetcher_source()]), settings)

        assert [(c.app.service, c.app.instance, c.action) for c in changes] == [
            (FETCHER, "crates-audit", "deploy")
        ]
        doc = changes[0].doc
        assert doc["deploy"] == {"service": FETCHER, "instance": "crates-audit"}
        assert doc["component"] == "fetcher-crates-audit"
        assert get_path(doc, "config.instance_id") == "crates-audit"
        assert get_path(doc, "config.sources") == {
            "crates_io": {
                "enabled": True,
                "topic": "crates-audit",
                "crates": ["dfe-fetcher"],
                "interval_secs": 3600,
            }
        }

    def test_the_main_topic_reaches_the_stanza(self, crud, settings):
        changes = derived.plan(crud, _Registry([_fetcher_source(topic="main")]), settings)
        assert get_path(changes[0].doc, "config.sources.crates_io.topic") == "main"

    @pytest.mark.parametrize(
        "source",
        [
            _fetcher_source(deployed=False),
            _fetcher_source(state="dormant"),
            _fetcher_source(state="disabled"),
            _receiver_source(),
        ],
    )
    def test_nothing_is_deployed_for_a_source_that_is_not_live_and_fetched(
        self, crud, settings, source
    ):
        changes = derived.plan(crud, _Registry([source]), settings)
        assert [c for c in changes if c.app.service == FETCHER] == []

    def test_an_instance_whose_source_went_dormant_is_removed(self, crud, settings):
        registry = _Registry([_fetcher_source()])
        _apply(crud, derived.plan(crud, registry, settings))

        changes = derived.plan(crud, _Registry([_fetcher_source(state="dormant")]), settings)

        assert [(c.app.instance, c.action) for c in changes] == [("crates-audit", "remove")]

    def test_an_instance_whose_source_was_deleted_is_removed(self, crud, settings):
        _apply(crud, derived.plan(crud, _Registry([_fetcher_source()]), settings))

        changes = derived.plan(crud, _Registry([]), settings)

        assert [(c.app.instance, c.action) for c in changes] == [("crates-audit", "remove")]

    def test_a_changed_stanza_is_synced_not_redeployed(self, crud, settings):
        _apply(crud, derived.plan(crud, _Registry([_fetcher_source()]), settings))
        changed = _fetcher_source()
        changed.versions["1.0.0"].fetcher.config["interval_secs"] = 60

        changes = derived.plan(crud, _Registry([changed]), settings)

        assert [(c.app.instance, c.action) for c in changes] == [("crates-audit", "sync")]
        assert get_path(changes[0].doc, "config.sources.crates_io.interval_secs") == 60

    def test_a_hand_edit_to_the_stanza_is_drift(self, crud, settings):
        registry = _Registry([_fetcher_source()])
        _apply(crud, derived.plan(crud, registry, settings))
        app = instances.instance_of(FETCHER, "crates-audit")
        doc = instances.read_overlay(crud, app)
        doc["config"]["sources"]["crates_io"]["topic"] = "elsewhere"
        _put(crud, app, doc)

        changes = derived.plan(crud, registry, settings)

        assert [c.action for c in changes] == ["sync"]
        assert get_path(changes[0].doc, "config.sources.crates_io.topic") == "crates-audit"

    def test_in_step_means_no_changes(self, crud, settings):
        registry = _Registry([_fetcher_source(), _receiver_source()])
        _apply(crud, derived.plan(crud, registry, settings))
        assert derived.plan(crud, registry, settings) == []

    def test_a_main_landing_source_gets_its_instance_straight_off_the_create(self, crud, settings):
        write = SourceWriteRequest.model_validate(
            {
                "source": "crates-main",
                "fetcher": {
                    "source_type": "crates_io",
                    "topic": "main",
                    "config": {"crates": ["dfe-fetcher"]},
                },
            }
        )
        source = source_from_write(write, source_name="crates-main")

        changes = derived.plan(crud, _Registry([source]), settings)

        assert [(c.app.service, c.app.instance, c.action) for c in changes] == [
            (FETCHER, "crates-main", "deploy")
        ]


class TestTransformInstances:
    """A transform instance is derived exactly like a fetcher instance."""

    def test_a_source_that_names_a_transform_gets_its_instance(self, crud, settings):
        source = _receiver_source(transform={"engine": "vrl"})

        changes = derived.plan(crud, _Registry([source]), settings)

        transform = next(c for c in changes if c.app.service == VRL)
        assert (transform.app.instance, transform.action) == ("filebeat", "deploy")
        assert get_path(transform.doc, "config.source.topics") == ["filebeat_land"]
        assert get_path(transform.doc, "config.sink.topic") == "filebeat_load"

    def test_only_the_app_that_runs_it_gets_an_instance(self, crud, settings):
        source = _receiver_source(transform={"engine": "vrl"})

        changes = derived.plan(crud, _Registry([source]), settings)

        assert [c.app.service for c in changes if c.app.service.startswith("dfe-transform-")] == [
            VRL
        ]

    def test_a_source_with_no_transform_deploys_none(self, crud, settings):
        changes = derived.plan(crud, _Registry([_receiver_source()]), settings)

        assert [c for c in changes if c.app.service == VRL] == []

    def test_an_instance_whose_source_dropped_its_transform_is_removed(self, crud, settings):
        _apply(
            crud,
            derived.plan(
                crud, _Registry([_receiver_source(transform={"engine": "vrl"})]), settings
            ),
        )

        changes = derived.plan(crud, _Registry([_receiver_source()]), settings)

        assert [(c.app.service, c.action) for c in changes] == [(VRL, "remove")]


class TestDescribe:
    def test_the_report_line_names_the_instance_and_the_action(self):
        change = derived.DerivedChange(instances.instance_of(FETCHER, "x"), "deploy", {})
        assert change.describe() == "dfe-fetcher/x: deploy instance"
