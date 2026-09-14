#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_app_contracts.py
#  Purpose:      Tests for the contract route and the per-instance config route
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The routes the console's app-settings page is built on: two reads and a write."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dfe_engine.appmgmt import appconfig, contract
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


def _etag(client, headers) -> str:
    """The revision the config route says a write should be made against."""
    return client.get(CONFIG, headers=headers).json()["etag"]


def _write(client, headers, changes: dict, etag: str | None = None):
    """PUT a set of changes, with If-Match unless the caller wants it left off."""
    sent = dict(headers)
    if etag is not None:
        sent["If-Match"] = etag
    return client.put(CONFIG, json={"changes": changes}, headers=sent)


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


class TestWritingConfig:
    def test_the_loader_done_when_round_trips(self, client, app, admin_headers, tmp_path):
        # #380's done-when: one override lands, reports provenance overlay on the
        # next read, and the app's own default is still shown beside it.
        gc = _wire(app, tmp_path)
        _deploy(client, admin_headers)
        resp = _write(
            client,
            admin_headers,
            {"config.batch_processing.max_chunk_size": 5000},
            _etag(client, admin_headers),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["changed"] is True
        assert resp.json()["commit_sha"]
        assert resp.json()["reload"] == "roll"
        assert gc.head_revision() == resp.json()["commit_sha"]

        body = client.get(CONFIG, headers=admin_headers).json()
        written = {f["path"]: f for f in body["fields"]}["config.batch_processing.max_chunk_size"]
        assert (written["provenance"], written["value"], written["default"]) == (
            "overlay",
            5000,
            10000,
        )

    def test_a_type_invalid_value_is_refused_before_anything_is_committed(
        self, client, app, admin_headers, tmp_path
    ):
        gc = _wire(app, tmp_path)
        _deploy(client, admin_headers)
        before = gc.head_revision()
        resp = _write(client, admin_headers, {"config.batch_processing.max_chunk_size": "lots"})
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "invalid_value"
        assert resp.json()["context"]["path"] == "config.batch_processing.max_chunk_size"
        assert gc.head_revision() == before

    def test_a_rust_enum_branch_the_app_does_not_declare_is_refused(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        resp = _write(client, admin_headers, {"config.clickhouse.insert_format": "csv"})
        assert resp.status_code == 400, resp.text
        assert "takes one of" in resp.json()["message"]

    def test_a_value_only_the_capability_catalogue_refuses_is_refused(
        self, client, app, admin_headers, tmp_path, monkeypatch
    ):
        # Written here rather than emitted: the one shipped catalogue enum that
        # names a declared option is clickhouse.protocol, which the deployment sets.
        mount = tmp_path / "contract" / LOADER
        mount.mkdir(parents=True)
        (mount / contract.SCHEMA_FILE).write_text(
            json.dumps(
                {
                    "type": "object",
                    "properties": {
                        "widget": {
                            "type": "object",
                            # A plain string in the schema, a closed set in the catalogue.
                            "properties": {"mode": {"type": "string"}},
                        }
                    },
                }
            )
        )
        (mount / contract.CAPABILITIES_FILE).write_text(
            json.dumps(
                [{"name": "widget", "fields": [{"name": "mode", "enum_values": ["fast", "slow"]}]}]
            )
        )
        monkeypatch.setenv(contract.CONTRACT_DIR_ENV, str(tmp_path / "contract"))
        contract.reload_contracts()

        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        assert _write(client, admin_headers, {"config.widget.mode": "fast"}).status_code == 200
        resp = _write(client, admin_headers, {"config.widget.mode": "medium"})
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "invalid_value"
        assert "'fast', 'slow'" in resp.json()["message"]

    def test_a_path_the_deployment_sets_is_refused_and_names_what_sets_it(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        resp = _write(client, admin_headers, {"config.clickhouse.hosts": ["ch-0:8123"]})
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "chart_derived"
        assert "DFE_LOADER_CLICKHOUSE_HOSTS" in resp.json()["message"]

    def test_a_secret_is_written_and_still_never_read_back(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        resp = _write(client, admin_headers, {"config.geoip.auto_download.ipinfo_token": "hunter2"})
        assert resp.status_code == 200, resp.text
        read = client.get(CONFIG, headers=admin_headers)
        token = {f["path"]: f for f in read.json()["fields"]}[
            "config.geoip.auto_download.ipinfo_token"
        ]
        assert (token["secret"], token["set"], token["value"]) == (True, True, None)
        assert "hunter2" not in read.text

    def test_an_if_match_that_has_gone_stale_is_refused(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        stale = _etag(client, admin_headers)
        _write(client, admin_headers, {"config.batch_processing.max_chunk_size": 5000}, stale)
        resp = _write(
            client, admin_headers, {"config.batch_processing.max_chunk_size": 6000}, stale
        )
        assert resp.status_code == 412, resp.text
        assert resp.json()["code"] == "stale_etag"

    def test_writing_config_needs_the_helmvars_write(
        self, client, app, viewer_headers, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        resp = _write(client, viewer_headers, {"config.batch_processing.max_chunk_size": 5000})
        assert resp.status_code == 403, resp.text

    def test_a_change_set_with_nothing_in_it_commits_nothing(
        self, client, app, admin_headers, tmp_path
    ):
        gc = _wire(app, tmp_path)
        _deploy(client, admin_headers)
        before = gc.head_revision()
        resp = _write(client, admin_headers, {})
        assert resp.status_code == 200, resp.text
        assert resp.json()["changed"] is False
        assert gc.head_revision() == before


class TestWritingCustomEnv:
    def test_a_custom_key_round_trips_and_is_then_removed(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        assert (
            _write(client, admin_headers, {"extraEnv.DFE_LOADER_HOUSE_KEY": "kept"}).status_code
            == 200
        )
        body = client.get(CONFIG, headers=admin_headers).json()
        assert body["custom"] == [{"path": "extraEnv.DFE_LOADER_HOUSE_KEY", "value": "kept"}]
        assert body["unknown"] == []

        assert (
            _write(client, admin_headers, {"extraEnv.DFE_LOADER_HOUSE_KEY": None}).status_code
            == 200
        )
        assert client.get(CONFIG, headers=admin_headers).json()["custom"] == []

    def test_a_custom_key_is_never_judged_against_the_app_schema(
        self, client, app, admin_headers, tmp_path
    ):
        # The block exists for names no contract declares, so an unknown one is
        # the normal case rather than the error case.
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        assert _write(client, admin_headers, {"extraEnv.NOT_IN_ANY_SCHEMA": "1"}).status_code == 200

    def test_a_name_that_is_not_an_environment_name_is_refused(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        resp = _write(client, admin_headers, {"extraEnv.lower-case": "no"})
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "invalid_env_name"

    def test_a_value_carrying_a_line_break_is_refused(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        resp = _write(client, admin_headers, {"extraEnv.KEY": "one\nTWO=smuggled"})
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "invalid_env_value"

    def test_a_change_that_addresses_neither_block_is_refused(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        resp = _write(client, admin_headers, {"replicaCount": 3})
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "invalid_path"

    def test_with_no_env_directory_the_chart_delivers_it(
        self, client, app, admin_headers, tmp_path
    ):
        # Kubernetes: the app's chart renders extraEnv, so the engine writes no file.
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        resp = _write(client, admin_headers, {"extraEnv.DFE_LOADER_HOUSE_KEY": "kept"})
        assert "chart renders extraEnv" in resp.json()["custom_env"]
        assert list(tmp_path.glob(f"**/*{appconfig.CUSTOM_ENV_SUFFIX}")) == []

    def test_on_docker_the_env_file_is_written_beside_the_app_config(
        self, client, app, admin_headers, tmp_path
    ):
        gc = _wire(app, tmp_path)
        app.state.settings.deployment.target = "docker"
        app.state.settings.deployment.app_config_dir = str(tmp_path / "app-config")
        app.state.settings.deployment.app_env_dir = str(tmp_path / "app-env")
        _deploy(client, admin_headers)
        resp = _write(
            client,
            admin_headers,
            {"extraEnv.DFE_LOADER_HOUSE_KEY": "kept", "extraEnv.SECOND": "also"},
        )
        assert resp.status_code == 200, resp.text

        written = tmp_path / "app-env" / f"{LOADER}{appconfig.CUSTOM_ENV_SUFFIX}"
        assert written.read_text() == "DFE_LOADER_HOUSE_KEY=kept\nSECOND=also\n"
        # An operator writes credentials here, so nobody else on the host reads it.
        assert written.stat().st_mode & 0o777 == 0o600
        # Compose reads env_file at up time, so a restart would keep the old set.
        assert resp.json()["restart_required"] == [
            f"recreate required: docker compose up -d {LOADER}"
        ]
        assert "app environment directory" in resp.json()["custom_env"]
        # Nothing rolls a pod on a Compose stack, so the command is the answer.
        assert resp.json()["reload"] == "restart"
        assert gc.head_revision() == resp.json()["commit_sha"]

    def test_removing_the_last_custom_key_empties_the_file(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        app.state.settings.deployment.target = "docker"
        app.state.settings.deployment.app_config_dir = str(tmp_path / "app-config")
        app.state.settings.deployment.app_env_dir = str(tmp_path / "app-env")
        _deploy(client, admin_headers)
        _write(client, admin_headers, {"extraEnv.DFE_LOADER_HOUSE_KEY": "kept"})
        _write(client, admin_headers, {"extraEnv.DFE_LOADER_HOUSE_KEY": None})
        written = tmp_path / "app-env" / f"{LOADER}{appconfig.CUSTOM_ENV_SUFFIX}"
        assert written.read_text() == ""
