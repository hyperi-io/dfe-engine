#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_app_contracts.py
#  Purpose:      Tests for the contract route and the per-instance config route
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The two read routes the console's app-settings page is built on."""

from __future__ import annotations

from pathlib import Path

import pytest

from dfe_engine.appmgmt import contract
from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance import PolicyStore

FIXTURES = Path(__file__).parents[2] / "fixtures" / "contract"

LOADER = "dfe-loader"
CONFIG = f"/api/v1/apps/{LOADER}/default/config"
CONTRACT = f"/api/v1/app-contracts/{LOADER}"


@pytest.fixture(autouse=True)
def _mounted(monkeypatch):
    """Point the reader at the emitted contracts, as a deployment's mount does."""
    monkeypatch.setenv(contract.CONTRACT_DIR_ENV, str(FIXTURES))
    contract.reload_contracts()
    yield
    contract.reload_contracts()


@pytest.fixture
def _unmounted(monkeypatch):
    """A deployment whose emit step has not run."""
    monkeypatch.setenv(contract.CONTRACT_DIR_ENV, "/nonexistent/contract")
    contract.reload_contracts()


def _wire(app, tmp_path):
    """Attach a real local-repo GitCrud, as the app-management tests do."""
    app.state.settings.env = "dev"
    app.state.settings.deployment.target = "kubernetes"
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    gc = GitCrud(repo, default_registry())
    app.state.gitcrud = gc
    app.state.policy_store = PolicyStore(gc)
    return gc


def _deploy(client, headers, values: dict | None = None):
    """Stand up the loader, which is a single-instance app named `default`."""
    return client.post(
        f"/api/v1/apps/{LOADER}/instances",
        json={"instance": "default", "values": values or {}},
        headers=headers,
    )


class TestTheContractRoute:
    def test_it_serves_what_the_image_emitted(self, client, admin_headers):
        body = client.get(CONTRACT, headers=admin_headers).json()
        assert body["service"] == LOADER
        assert (body["available"], body["source"]) == (True, "mount")
        assert body["schema_version"] == "https://json-schema.org/draft/2020-12/schema"
        assert len(body["schema"]["properties"]) == 24
        assert [c["name"] for c in body["capabilities"]] == ["clickhouse", "pipeline"]

    def test_dials_are_deferred_and_say_so_by_being_empty(self, client, admin_headers):
        assert client.get(CONTRACT, headers=admin_headers).json()["dials"] == []

    @pytest.mark.usefixtures("_unmounted")
    def test_nothing_mounted_is_an_answer_rather_than_a_500(self, client, admin_headers):
        resp = client.get(CONTRACT, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["available"] is False
        assert resp.json()["source"] == "absent"

    def test_an_app_the_manifest_does_not_declare_is_a_404(self, client, admin_headers):
        resp = client.get("/api/v1/app-contracts/dfe-nonesuch", headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "unknown_app"

    def test_reading_a_contract_is_a_deployment_read(self, client, viewer_headers):
        # It says what an app CAN be asked to do and carries no instance's values.
        assert client.get(CONTRACT, headers=viewer_headers).status_code == 200


class TestTheConfigRoute:
    def test_every_option_comes_back_whether_or_not_it_is_set(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        body = client.get(CONFIG, headers=admin_headers).json()
        assert body["available"] is True
        assert len(body["fields"]) == 175
        assert body["etag"]

    def test_the_chart_derived_key_is_reported_as_the_chart_s(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        body = client.get(CONFIG, headers=admin_headers).json()
        by_path = {f["path"]: f for f in body["fields"]}
        assert by_path["config.clickhouse.protocol"]["provenance"] == "chart"

    def test_a_written_value_outranks_the_default_and_says_so(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _deploy(
            client, admin_headers, values={"config": {"batch_processing": {"max_chunk_size": 5000}}}
        )
        body = client.get(CONFIG, headers=admin_headers).json()
        by_path = {f["path"]: f for f in body["fields"]}
        written = by_path["config.batch_processing.max_chunk_size"]
        assert (written["provenance"], written["value"], written["default"]) == (
            "overlay",
            5000,
            10000,
        )
        assert written["set"] is True

    def test_a_secret_reports_only_whether_it_is_set(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _deploy(client, admin_headers, values={"config": {"clickhouse": {"password": "hunter2"}}})
        body = client.get(CONFIG, headers=admin_headers).json()
        by_path = {f["path"]: f for f in body["fields"]}
        secret = by_path["config.clickhouse.password"]
        assert (secret["secret"], secret["value"], secret["default"]) == (True, None, None)
        assert secret["set"] is True
        assert "hunter2" not in client.get(CONFIG, headers=admin_headers).text

    def test_an_overlay_key_the_contract_does_not_know_comes_back_as_unknown(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _deploy(client, admin_headers, values={"config": {"retired": {"key": "value"}}})
        body = client.get(CONFIG, headers=admin_headers).json()
        assert body["unknown"] == [{"path": "config.retired.key", "value": "value"}]

    @pytest.mark.usefixtures("_unmounted")
    def test_nothing_mounted_leaves_the_overlay_unexplained_rather_than_failing(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _deploy(client, admin_headers, values={"config": {"clickhouse": {"database": "dfe"}}})
        body = client.get(CONFIG, headers=admin_headers).json()
        assert (body["available"], body["fields"]) == (False, [])
        assert [u["path"] for u in body["unknown"]] == ["config.clickhouse.database"]

    def test_an_instance_that_is_not_deployed_is_a_404(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        resp = client.get(CONFIG, headers=admin_headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_deployed"

    def test_reading_an_instance_s_values_needs_the_helmvars_read(
        self, client, app, viewer_headers, admin_headers, tmp_path
    ):
        # infra_viewer carries deployment:read, so the contract is readable and
        # this is not - these are what an operator has actually configured.
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        assert client.get(CONFIG, headers=viewer_headers).status_code == 403
