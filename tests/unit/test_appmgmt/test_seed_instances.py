#  Project:      dfe-engine
#  File:         tests/unit/test_appmgmt/test_seed_instances.py
#  Purpose:      Tests for seeding a deployment's default app instances
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Seeding the default app set into a deploy repo that carries none."""

from __future__ import annotations

import pytest

from dfe_engine.appmgmt import instances, seed_instances
from dfe_engine.appmgmt.catalogue import Multiplicity, descriptor
from dfe_engine.gitcrud.engine import get_path
from dfe_engine.settings import DFESettings

PROFILE = "docker-slim"
ACTOR = "test"

# The apps a Compose slim deployment runs unasked. Spelled out rather than
# recomputed, so a manifest that stops seeding the receiver or the loader fails
# here instead of in a deployment whose flows refuse every shape.
EXPECTED = [
    "dfe-archiver",
    "dfe-engine",
    "dfe-loader",
    "dfe-receiver",
    "dfe-ui",
    "hyperdx",
]


def _settings(profile: str = PROFILE) -> DFESettings:
    return DFESettings(env="dev", deployment={"profile": profile})


def _marker(crud) -> list[str]:
    path = crud.repo_path / seed_instances.MARKER_FILE
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


def _deployed(crud) -> list[str]:
    return [i.service for i in instances.list_instances(crud)]


class TestSeedDefaultInstances:
    def test_a_fresh_repo_gets_the_profiles_default_apps(self, crud):
        seeded = seed_instances.seed_default_instances(crud, _settings())

        assert seeded == EXPECTED
        assert _deployed(crud) == EXPECTED
        assert _marker(crud) == EXPECTED

    def test_every_seeded_overlay_names_the_deployment_it_makes(self, crud):
        seed_instances.seed_default_instances(crud, _settings())

        for service in EXPECTED:
            app = instances.instance_of(service, seed_instances.SEED_INSTANCE)
            doc = instances.read_overlay(crud, app)
            assert get_path(doc, "deploy.service") == service
            assert get_path(doc, "deploy.instance") == seed_instances.SEED_INSTANCE
            assert get_path(doc, "otelServiceName") == app.telemetry_name

    def test_only_single_deployment_apps_are_seeded(self, crud):
        seeded = seed_instances.seed_default_instances(crud, _settings())

        assert "dfe-fetcher" not in seeded
        assert all(descriptor(s).multiplicity is Multiplicity.SINGLE for s in seeded)

    def test_a_second_start_seeds_nothing(self, crud):
        seed_instances.seed_default_instances(crud, _settings())
        head = crud.head_revision()

        assert seed_instances.seed_default_instances(crud, _settings()) == []
        assert crud.head_revision() == head

    def test_an_app_the_operator_removed_is_not_seeded_again(self, crud):
        seed_instances.seed_default_instances(crud, _settings())
        app = instances.instance_of("dfe-archiver", seed_instances.SEED_INSTANCE)
        crud.delete(instances.HELMVARS_CLASS, app.overlay_name, ACTOR, message="cfg(x): remove")

        assert seed_instances.seed_default_instances(crud, _settings()) == []
        assert not instances.exists(crud, app)
        assert "dfe-archiver" in _marker(crud)

    def test_a_repo_with_no_marker_adopts_the_overlays_it_has(self, crud):
        app = instances.instance_of("dfe-receiver", seed_instances.SEED_INSTANCE)
        doc = instances.initial_overlay(app, {"replicaCount": 3})
        crud.put(instances.HELMVARS_CLASS, app.overlay_name, doc, ACTOR, message="cfg(x): put")

        seeded = seed_instances.seed_default_instances(crud, _settings())

        assert "dfe-receiver" not in seeded
        assert get_path(instances.read_overlay(crud, app), "replicaCount") == 3
        assert _marker(crud) == EXPECTED

    @pytest.mark.parametrize("profile", ["", "   "])
    def test_a_deployment_with_no_profile_seeds_nothing(self, crud, profile):
        assert seed_instances.seed_default_instances(crud, _settings(profile)) == []
        assert _deployed(crud) == []
        assert _marker(crud) == []


class TestDefaultServices:
    def test_an_optional_app_is_no_profiles_default(self):
        assert descriptor("culvert").optional
        assert "culvert" not in seed_instances.default_services("scale")
