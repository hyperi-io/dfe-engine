#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_source_name_budget.py
#  Purpose:      Tests that a source's name fits the per-config apps it deploys
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A source that deploys a per-config app has to fit that app's Helm release name.

The regression: a source whose transform instance named a Helm release past
Helm's 53 characters was stored with a 200, and Argo CD then refused the
Application with nothing telling the operator. Every path that stores a source
the reconcile would deploy an instance for runs the same gate.
"""

from pathlib import Path

import pytest

from dfe_engine.api.deps import _registries
from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance import PolicyStore
from dfe_engine.settings import reset_settings
from dfe_engine.source import catalogue as sc
from dfe_engine.source.registry import SourceRegistry

VECTOR = "dfe-transform-vector"
VECTOR_BUDGET = 12
ELASTIC = "dfe-transform-elastic"
ELASTIC_BUDGET = 11
FETCHER = "dfe-fetcher"
FETCHER_BUDGET = 21

CATALOGUE = Path(__file__).parents[2] / "fixtures" / "catalogue" / "sources.yaml"


@pytest.fixture
def target(monkeypatch):
    """Set the deploy target the source save path reads, and drop it after."""

    def point(name: str, **env: str) -> None:
        monkeypatch.setenv("DFE_DEPLOYMENT_TARGET", name)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        reset_settings()

    yield point
    reset_settings()


@pytest.fixture
def mounted(monkeypatch):
    """A deployment with the catalogue mounted, and none held after the test."""
    monkeypatch.setenv(sc.CATALOGUE_FILE_ENV, str(CATALOGUE))
    sc.reload_source_catalogue()
    yield
    monkeypatch.delenv(sc.CATALOGUE_FILE_ENV, raising=False)
    sc.reload_source_catalogue()


def _wire(app, tmp_path, deploy_target: str = "kubernetes") -> GitCrud:
    """A real local deploy repo, holding the sources the way a gitops deployment does."""
    app.state.settings.env = "dev"
    app.state.settings.deployment.target = deploy_target
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    gc = GitCrud(repo, default_registry())
    app.state.gitcrud = gc
    app.state.policy_store = PolicyStore(gc)
    _registries["source"] = SourceRegistry(crud=gc)
    return gc


def _receiver(name: str, **extra) -> dict:
    return {"source": name, "match": {"field": "_json.app", "value": name}, **extra}


def _vector(name: str, **extra) -> dict:
    return _receiver(name, transform={"engine": "vector"}, **extra)


def _fetcher(name: str) -> dict:
    return {
        "source": name,
        "fetcher": {"source_type": "crates_io", "config": {"crates": ["dfe-fetcher"]}},
    }


def _assert_refused(resp, name: str, service: str, budget: int) -> None:
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "validation_error"
    message = resp.json()["message"]
    assert f"source name {name!r} is {len(name)} characters" in message
    assert f"at most {budget} for {service}" in message


class TestCreate:
    def test_a_transform_source_over_the_budget_is_422_and_not_stored(
        self, client, app, admin_headers, tmp_path, target
    ):
        target("kubernetes")
        _wire(app, tmp_path)

        resp = client.post("/api/v1/sources", json=_vector("umbrellavector"), headers=admin_headers)

        _assert_refused(resp, "umbrellavector", VECTOR, VECTOR_BUDGET)
        stored = client.get("/api/v1/sources/umbrellavector", headers=admin_headers)
        assert stored.status_code == 404

    def test_a_transform_source_at_the_budget_is_created(
        self, client, app, admin_headers, tmp_path, target
    ):
        target("kubernetes")
        _wire(app, tmp_path)
        name = "v" * VECTOR_BUDGET

        resp = client.post("/api/v1/sources", json=_vector(name), headers=admin_headers)

        assert resp.status_code == 201, resp.text

    def test_a_fetcher_source_takes_the_fetchers_budget(
        self, client, app, admin_headers, tmp_path, target
    ):
        target("kubernetes")
        _wire(app, tmp_path)
        over = "f" * (FETCHER_BUDGET + 1)
        at = "f" * FETCHER_BUDGET

        refused = client.post("/api/v1/sources", json=_fetcher(over), headers=admin_headers)
        created = client.post("/api/v1/sources", json=_fetcher(at), headers=admin_headers)

        _assert_refused(refused, over, FETCHER, FETCHER_BUDGET)
        assert created.status_code == 201, created.text

    def test_a_source_that_deploys_no_per_config_app_keeps_the_whole_label(
        self, client, app, admin_headers, tmp_path, target
    ):
        target("kubernetes")
        _wire(app, tmp_path)

        resp = client.post("/api/v1/sources", json=_receiver("r" * 40), headers=admin_headers)

        assert resp.status_code == 201, resp.text

    def test_an_unknown_target_is_held_to_it(self, client, app, admin_headers, tmp_path, target):
        target("unknown")
        _wire(app, tmp_path, "unknown")

        resp = client.post("/api/v1/sources", json=_vector("umbrellavector"), headers=admin_headers)

        _assert_refused(resp, "umbrellavector", VECTOR, VECTOR_BUDGET)

    def test_compose_renders_no_helm_release_so_takes_no_budget(
        self, client, app, admin_headers, tmp_path, target
    ):
        config_dir = str(tmp_path / "app-config")
        target("docker", DFE_DEPLOYMENT_APP_CONFIG_DIR=config_dir)
        _wire(app, tmp_path, "docker")
        app.state.settings.deployment.app_config_dir = config_dir

        resp = client.post("/api/v1/sources", json=_vector("v" * 40), headers=admin_headers)

        assert resp.status_code == 201, resp.text


class TestUpdate:
    def test_binding_a_stored_name_to_a_transform_it_cannot_carry_is_refused(
        self, client, app, admin_headers, tmp_path, target
    ):
        target("kubernetes")
        _wire(app, tmp_path)
        name = "r" * 20
        created = client.post("/api/v1/sources", json=_receiver(name), headers=admin_headers)
        assert created.status_code == 201, created.text

        resp = client.put(f"/api/v1/sources/{name}", json=_vector(name), headers=admin_headers)

        _assert_refused(resp, name, VECTOR, VECTOR_BUDGET)
        stored = client.get(f"/api/v1/sources/{name}", headers=admin_headers).json()
        assert stored["transform"] is None

    def test_activating_a_disabled_source_over_the_budget_is_refused(
        self, client, app, admin_headers, tmp_path, target
    ):
        # A disabled source deploys nothing, so it is stored; activating it would.
        target("kubernetes")
        _wire(app, tmp_path)
        created = client.post(
            "/api/v1/sources",
            json=_vector("umbrellavector", state="disabled"),
            headers=admin_headers,
        )
        assert created.status_code == 201, created.text

        resp = client.patch(
            "/api/v1/sources/umbrellavector", json={"state": "active"}, headers=admin_headers
        )

        _assert_refused(resp, "umbrellavector", VECTOR, VECTOR_BUDGET)


class TestImport:
    def _bundle(self, client, headers, name: str) -> dict:
        """A real export, re-addressed to *name* with its own match value."""
        created = client.post("/api/v1/sources", json=_vector("vsrc"), headers=headers)
        assert created.status_code == 201, created.text
        bundle = client.get("/api/v1/sources/vsrc/export", headers=headers).json()
        bundle["source"] = name
        bundle["routing"]["value"] = name
        return bundle

    def test_a_bundle_over_the_budget_is_refused_before_anything_lands(
        self, client, app, admin_headers, tmp_path, target
    ):
        target("kubernetes")
        _wire(app, tmp_path)
        bundle = self._bundle(client, admin_headers, "umbrellavector")

        resp = client.post("/api/v1/sources/import", json=bundle, headers=admin_headers)

        _assert_refused(resp, "umbrellavector", VECTOR, VECTOR_BUDGET)
        stored = client.get("/api/v1/sources/umbrellavector", headers=admin_headers)
        assert stored.status_code == 404

    def test_a_bundle_at_the_budget_imports(self, client, app, admin_headers, tmp_path, target):
        target("kubernetes")
        _wire(app, tmp_path)
        name = "v" * VECTOR_BUDGET
        bundle = self._bundle(client, admin_headers, name)

        resp = client.post("/api/v1/sources/import", json=bundle, headers=admin_headers)

        assert resp.status_code == 201, resp.text


class TestFromCatalogue:
    def test_an_entry_whose_derived_name_is_over_the_budget_is_refused(
        self, client, app, admin_headers, tmp_path, target, mounted
    ):
        target("kubernetes")
        _wire(app, tmp_path)

        resp = client.post(
            "/api/v1/sources/from-catalogue/cisco_meraki",
            json={"intake": "beats"},
            headers=admin_headers,
        )

        _assert_refused(resp, "cisco-meraki", ELASTIC, ELASTIC_BUDGET)

    def test_the_same_entry_under_a_name_that_fits_is_created(
        self, client, app, admin_headers, tmp_path, target, mounted
    ):
        target("kubernetes")
        _wire(app, tmp_path)

        resp = client.post(
            "/api/v1/sources/from-catalogue/cisco_meraki",
            json={"intake": "beats", "name": "meraki"},
            headers=admin_headers,
        )

        assert resp.status_code == 201, resp.text
        assert resp.json()["source"] == "meraki"
