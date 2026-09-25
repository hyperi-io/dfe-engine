#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_app_contracts.py
#  Purpose:      Tests for the contract route and the per-instance config route
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The routes the console's app-settings page is built on: two reads and a write."""

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

ARCHIVER = "dfe-archiver"
ARCHIVER_CONFIG = f"/api/v1/apps/{ARCHIVER}/default/config"


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


def _deploy_archiver(client, headers, values: dict | None = None):
    """Stand up the archiver, which is also a single-instance app named `default`."""
    return client.post(
        f"/api/v1/apps/{ARCHIVER}/instances",
        json={"instance": "default", "values": values or {}},
        headers=headers,
    )


def _write_archiver(client, headers, changes: dict, etag: str | None = None):
    sent = dict(headers)
    if etag is not None:
        sent["If-Match"] = etag
    return client.put(ARCHIVER_CONFIG, json={"changes": changes}, headers=sent)


def _live(client, headers, name: str, body: dict):
    """Define a source and mark it deployed, as a source deploy leaves it."""
    from dfe_engine.api.deps import _registries

    assert client.post("/api/v1/sources", json=body, headers=headers).status_code == 201
    _registries["source"].set_deployed_version(name, "1.0.0")


def _deploy_fetcher(client, headers, instance: str):
    """Stand up a fetcher instance, which is named for a fetcher-based source."""
    _live(
        client,
        headers,
        instance,
        {
            "source": instance,
            "fetcher": {"source_type": "crates_io", "config": {"crates": ["dfe-fetcher"]}},
        },
    )
    return client.post(
        "/api/v1/apps/dfe-fetcher/instances",
        json={"instance": instance},
        headers=headers,
    )


def _deploy_vrl(client, headers, instance: str):
    """Stand up a vrl transform instance, which is a source's processing step."""
    _live(
        client,
        headers,
        instance,
        {
            "source": instance,
            "match": {"field": "tags.collector.type", "value": instance},
            "transform": {"engine": "vrl"},
        },
    )
    return client.post(
        "/api/v1/apps/dfe-transform-vrl/instances",
        json={"instance": instance},
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
        _deploy(client, admin_headers, values={"config": {"retired": {"setting": "value"}}})
        body = client.get(CONFIG, headers=admin_headers).json()
        assert body["unknown"] == [{"path": "config.retired.setting", "value": "value"}]

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


class TestArchiverChartDerivedPaths:
    """The archiver reads its own bare env family, not DFE_ARCHIVER_* -- #381."""

    def test_a_kafka_path_is_refused_and_names_the_bare_env_var(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _deploy_archiver(client, admin_headers)
        resp = _write_archiver(client, admin_headers, {"config.kafka.brokers": ["k:9092"]})
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "chart_derived"
        assert "KAFKA_BROKERS" in resp.json()["message"]

    def test_a_transport_path_is_refused_and_names_the_bare_env_var(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _deploy_archiver(client, admin_headers)
        resp = _write_archiver(client, admin_headers, {"config.transport": "grpc"})
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "chart_derived"
        assert "ARCHIVER_TRANSPORT" in resp.json()["message"]

    def test_an_s3_path_is_refused_and_names_the_bare_env_var(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _deploy_archiver(client, admin_headers)
        resp = _write_archiver(client, admin_headers, {"config.archive.s3.bucket": "landing"})
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "chart_derived"
        assert "S3_BUCKET" in resp.json()["message"]

    def test_a_dlq_path_is_refused_and_names_the_bare_env_var(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _deploy_archiver(client, admin_headers)
        resp = _write_archiver(client, admin_headers, {"config.dlq.mode": "fan_out"})
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "chart_derived"
        assert "DLQ_MODE" in resp.json()["message"]


class TestCustomEnvCannotShadowAChartSetName:
    """A chart-set name written under extraEnv never reaches the app -- dfe-infra#314."""

    def _put(self, client, headers, service, instance, name):
        return client.put(
            f"/api/v1/apps/{service}/{instance}/config",
            json={"changes": {f"extraEnv.{name}": "mine"}},
            headers=headers,
        )

    def _assert_refused(self, resp, name):
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "chart_set_env"
        assert name in resp.json()["message"]

    def test_the_loader_chart_s_broker_list_cannot_be_shadowed(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        name = "DFE_LOADER_KAFKA_BROKERS"
        self._assert_refused(self._put(client, admin_headers, LOADER, "default", name), name)

    def test_the_receiver_chart_s_bind_address_cannot_be_shadowed(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        client.post(
            "/api/v1/apps/dfe-receiver/instances",
            json={"instance": "default"},
            headers=admin_headers,
        )
        name = "DFE_RECEIVER_BIND_ADDRESS"
        self._assert_refused(
            self._put(client, admin_headers, "dfe-receiver", "default", name), name
        )

    def test_the_fetcher_chart_s_sasl_user_cannot_be_shadowed(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _deploy_fetcher(client, admin_headers, "alpha")
        name = "DFE_FETCHER_KAFKA_SASL_USER"
        self._assert_refused(self._put(client, admin_headers, "dfe-fetcher", "alpha", name), name)

    def test_the_transform_chart_s_source_topics_cannot_be_shadowed(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _deploy_vrl(client, admin_headers, "edge")
        name = "DFE_TRANSFORM_SOURCE_TOPICS"
        self._assert_refused(
            self._put(client, admin_headers, "dfe-transform-vrl", "edge", name), name
        )

    def test_a_name_no_chart_sets_is_still_the_operator_s(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        resp = self._put(client, admin_headers, LOADER, "default", "DFE_LOADER_HOUSE_KEY")
        assert resp.status_code == 200, resp.text


class TestWritingCustomEnv:
    def test_a_custom_key_round_trips_and_is_then_removed(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _deploy(client, admin_headers)
        assert (
            _write(client, admin_headers, {"extraEnv.DFE_LOADER_HOUSE_STYLE": "kept"}).status_code
            == 200
        )
        body = client.get(CONFIG, headers=admin_headers).json()
        assert body["custom"] == [{"path": "extraEnv.DFE_LOADER_HOUSE_STYLE", "value": "kept"}]
        assert body["unknown"] == []

        assert (
            _write(client, admin_headers, {"extraEnv.DFE_LOADER_HOUSE_STYLE": None}).status_code
            == 200
        )
        assert client.get(CONFIG, headers=admin_headers).json()["custom"] == []

    def test_a_custom_credential_is_written_and_never_read_back(
        self, client, app, admin_headers, tmp_path
    ):
        gc = _wire(app, tmp_path)
        _deploy(client, admin_headers)
        resp = _write(client, admin_headers, {"extraEnv.DFE_LOADER_S3_SECRET": "s3-secret"})
        assert resp.status_code == 200, resp.text
        assert "s3-secret" not in resp.text

        config = client.get(CONFIG, headers=admin_headers)
        assert config.json()["custom"] == [
            {"path": "extraEnv.DFE_LOADER_S3_SECRET", "value": contract.REDACTED}
        ]
        values = client.get(f"/api/v1/apps/{LOADER}/default/values", headers=admin_headers)
        assert values.json()["values"]["extraEnv"] == {"DFE_LOADER_S3_SECRET": contract.REDACTED}
        for read in (config, values):
            assert "s3-secret" not in read.text
        # The container still needs the real value.
        doc = gc.get("helmvars", "dfe-loader-default-values")
        assert doc["extraEnv"]["DFE_LOADER_S3_SECRET"] == "s3-secret"

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
            f"recreate required: make apply SERVICES={LOADER}"
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


class TestANewInstanceRefusesTheMask:
    """A new instance stores nothing, so the mask in its values has nothing to mean."""

    def test_a_masked_read_redeployed_is_refused(self, client, app, admin_headers, tmp_path):
        # Undeploy, then deploy again from what /values showed: the password is the mask.
        gc = _wire(app, tmp_path)
        _deploy(
            client, admin_headers, values={"config": {"clickhouse": {"password": "ch-pw-4410"}}}
        )
        shown = client.get(f"/api/v1/apps/{LOADER}/default/values", headers=admin_headers).json()
        assert shown["values"]["config"]["clickhouse"]["password"] == contract.REDACTED
        assert client.delete(f"/api/v1/apps/{LOADER}/default", headers=admin_headers).is_success

        before = gc.head_revision()
        resp = _deploy(client, admin_headers, values={"config": shown["values"]["config"]})
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "masked_value"
        assert resp.json()["context"]["path"] == "config"
        assert gc.head_revision() == before
        assert "dfe-loader-default-values" not in gc.list("helmvars")

    def test_a_masked_dot_path_is_refused(self, client, app, admin_headers, tmp_path):
        gc = _wire(app, tmp_path)
        before = gc.head_revision()
        resp = _deploy(
            client, admin_headers, values={"config.clickhouse.password": contract.REDACTED}
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "masked_value"
        assert gc.head_revision() == before

    def test_a_real_value_still_deploys(self, client, app, admin_headers, tmp_path):
        gc = _wire(app, tmp_path)
        resp = _deploy(client, admin_headers, values={"config.clickhouse.password": "ch-pw-4410"})
        assert resp.status_code == 200, resp.text
        stored = gc.get("helmvars", "dfe-loader-default-values")
        assert stored["config"]["clickhouse"]["password"] == "ch-pw-4410"


RECEIVER = "dfe-receiver"
RECEIVER_BASE = f"/api/v1/apps/{RECEIVER}/default"

# Schemas from the builds that hold each source acknowledgement until delivery.
HELD = Path(__file__).parents[2] / "fixtures" / "contract-acknowledgements"

CREDENTIALS = {
    "config.server.auth.mode": "bearer",
    "config.server.auth.bearer.tokens": ["tok-1", "tok-2"],
    "config.server.auth.accepted_headers": [{"name": "x-api-key", "values": ["hv-1"]}],
    "config.server.auth.header_values": ["legacy-1"],
}
PLAINTEXT = ("tok-1", "tok-2", "hv-1", "legacy-1")


@pytest.fixture
def _held(monkeypatch):
    """Mount the schemas that carry the acknowledgements key and the secret marker."""
    monkeypatch.setenv(contract.CONTRACT_DIR_ENV, str(HELD))
    contract.reload_contracts()


def _deploy_receiver(client, headers):
    return client.post(
        f"/api/v1/apps/{RECEIVER}/instances",
        json={"instance": "default"},
        headers=headers,
    )


def _put_receiver(client, headers, changes: dict):
    return client.put(f"{RECEIVER_BASE}/config", json={"changes": changes}, headers=headers)


@pytest.mark.usefixtures("_held")
class TestTurningAcknowledgementsOff:
    @pytest.mark.parametrize("block", ["server", "grpc", "otlp", "webhook"])
    def test_the_receiver_takes_it_and_reads_it_back(
        self, client, app, admin_headers, tmp_path, block
    ):
        _wire(app, tmp_path)
        assert _deploy_receiver(client, admin_headers).status_code == 200
        path = f"config.{block}.acknowledgements.enabled"
        resp = _put_receiver(client, admin_headers, {path: False})
        assert resp.status_code == 200, resp.text
        assert resp.json()["changed"] is True

        body = client.get(f"{RECEIVER_BASE}/config", headers=admin_headers).json()
        held = {f["path"]: f for f in body["fields"]}[path]
        assert (held["value"], held["provenance"]) == (False, "overlay")

    def test_a_string_where_the_app_takes_a_boolean_is_refused(
        self, client, app, admin_headers, tmp_path
    ):
        gc = _wire(app, tmp_path)
        _deploy_receiver(client, admin_headers)
        before = gc.head_revision()
        resp = _put_receiver(
            client, admin_headers, {"config.server.acknowledgements.enabled": "off"}
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "invalid_value"
        assert gc.head_revision() == before


@pytest.mark.usefixtures("_held")
class TestCredentialsAreNotEchoed:
    def test_the_write_response_carries_none_of_them(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _deploy_receiver(client, admin_headers)
        resp = _put_receiver(client, admin_headers, CREDENTIALS)
        assert resp.status_code == 200, resp.text
        for credential in PLAINTEXT:
            assert credential not in resp.text

    def test_the_values_route_masks_tokens_and_header_values(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _deploy_receiver(client, admin_headers)
        assert _put_receiver(client, admin_headers, CREDENTIALS).status_code == 200

        resp = client.get(f"{RECEIVER_BASE}/values", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        auth = resp.json()["values"]["config"]["server"]["auth"]
        assert auth["bearer"]["tokens"] == [contract.REDACTED, contract.REDACTED]
        assert auth["accepted_headers"] == [{"name": "x-api-key", "values": [contract.REDACTED]}]
        assert auth["header_values"] == [contract.REDACTED]
        assert auth["mode"] == "bearer"
        for credential in PLAINTEXT:
            assert credential not in resp.text

    def test_the_config_route_reports_them_set_and_says_nothing_else(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _deploy_receiver(client, admin_headers)
        _put_receiver(client, admin_headers, CREDENTIALS)

        resp = client.get(f"{RECEIVER_BASE}/config", headers=admin_headers)
        by_path = {f["path"]: f for f in resp.json()["fields"]}
        for path in ("config.server.auth.bearer.tokens", "config.server.auth.header_values"):
            assert (by_path[path]["secret"], by_path[path]["set"]) == (True, True), path
            assert by_path[path]["value"] is None, path
        # A header's name is a setting; only its values are the credential.
        headers = by_path["config.server.auth.accepted_headers"]
        assert (headers["secret"], headers["set"]) == (False, True)
        assert headers["value"] == [{"name": "x-api-key", "values": [contract.REDACTED]}]
        for credential in PLAINTEXT:
            assert credential not in resp.text

    def test_a_fetcher_map_entry_shows_and_its_token_survives_a_write_back(
        self, client, app, admin_headers, tmp_path
    ):
        gc = _wire(app, tmp_path)
        _deploy_fetcher(client, admin_headers, "alpha")
        base = "/api/v1/apps/dfe-fetcher/alpha"
        entry = {"url": "https://api.example", "topic": "t", "auth": {"token": "rest-tok-7731"}}
        resp = client.put(
            f"{base}/config",
            json={"changes": {"config.sources.rest": {"primary": entry}}},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text

        read = client.get(f"{base}/config", headers=admin_headers)
        field = {f["path"]: f for f in read.json()["fields"]}["config.sources.rest"]
        shown = field["value"]["primary"]
        assert shown["url"] == "https://api.example"
        assert shown["auth"]["token"] == contract.REDACTED
        assert "rest-tok-7731" not in read.text

        shown["topic"] = "t-2"
        written = client.put(
            f"{base}/config",
            json={"changes": {"config.sources.rest": {"primary": shown}}},
            headers=admin_headers,
        )
        assert written.status_code == 200, written.text
        stored = gc.get("helmvars", "dfe-fetcher-alpha-values")["config"]["sources"]["rest"]
        assert stored == {"primary": {**entry, "topic": "t-2"}}

    def test_the_fetcher_ingest_token_is_masked_on_both_reads(
        self, client, app, admin_headers, tmp_path
    ):
        # The schema carries no marker on it, so the name rule is what hides it.
        _wire(app, tmp_path)
        _deploy_fetcher(client, admin_headers, "alpha")
        base = "/api/v1/apps/dfe-fetcher/alpha"
        path = "config.ingest.auth_token"
        resp = client.put(
            f"{base}/config", json={"changes": {path: "ingest-tok-6620"}}, headers=admin_headers
        )
        assert resp.status_code == 200, resp.text

        config = client.get(f"{base}/config", headers=admin_headers)
        field = {f["path"]: f for f in config.json()["fields"]}[path]
        assert (field["secret"], field["set"], field["value"]) == (True, True, None)
        assert "ingest-tok-6620" not in config.text
        values = client.get(f"{base}/values", headers=admin_headers)
        assert values.json()["values"]["config"]["ingest"]["auth_token"] == contract.REDACTED
        assert "ingest-tok-6620" not in values.text

    def test_deleting_a_fetcher_connection_leaves_each_token_on_its_own_account(
        self, client, app, admin_headers, tmp_path
    ):
        gc = _wire(app, tmp_path)
        _deploy_fetcher(client, admin_headers, "alpha")
        base = "/api/v1/apps/dfe-fetcher/alpha"
        path = "config.sources.github.connections"
        connections = [{"id": n, "org": f"org-{n}", "token": f"gh-{n}-5521"} for n in "abc"]
        resp = client.put(
            f"{base}/config", json={"changes": {path: connections}}, headers=admin_headers
        )
        assert resp.status_code == 200, resp.text

        read = client.get(f"{base}/config", headers=admin_headers)
        shown = {f["path"]: f for f in read.json()["fields"]}[path]["value"]
        assert [entry["token"] for entry in shown] == [contract.REDACTED] * 3
        written = client.put(
            f"{base}/config",
            json={"changes": {path: [shown[0], shown[2]]}},
            headers=admin_headers,
        )
        assert written.status_code == 200, written.text
        stored = gc.get("helmvars", "dfe-fetcher-alpha-values")["config"]["sources"]["github"]
        assert stored["connections"] == [connections[0], connections[2]]

    def test_the_helm_vars_route_masks_them_too(self, client, app, admin_headers, tmp_path):
        # The same overlay read through the generic helm-var surface, same privilege.
        _wire(app, tmp_path)
        _deploy_receiver(client, admin_headers)
        _put_receiver(
            client,
            admin_headers,
            {**CREDENTIALS, "extraEnv.DFE_RECEIVER_S3_SECRET": "env-secret"},
        )

        resp = client.get(
            "/api/v1/helm/files/dfe-receiver-default-values/vars", headers=admin_headers
        )
        assert resp.status_code == 200, resp.text
        by_path = {v["path"]: v["value"] for v in resp.json()}
        assert by_path["config.server.auth.bearer.tokens[0]"] == contract.REDACTED
        assert by_path["config.server.auth.accepted_headers[0].name"] == "x-api-key"
        assert by_path["config.server.auth.accepted_headers[0].values[0]"] == contract.REDACTED
        assert by_path["config.server.auth.header_values[0]"] == contract.REDACTED
        assert by_path["extraEnv.DFE_RECEIVER_S3_SECRET"] == contract.REDACTED
        assert by_path["config.server.auth.mode"] == "bearer"
        for credential in (*PLAINTEXT, "env-secret"):
            assert credential not in resp.text

    def test_a_masked_read_written_back_keeps_the_stored_credentials(
        self, client, app, admin_headers, tmp_path
    ):
        # A client that reads /values, edits one field and writes the rest back
        # unchanged hands the engine the mask where the credentials were.
        gc = _wire(app, tmp_path)
        _deploy_receiver(client, admin_headers)
        _put_receiver(
            client, admin_headers, {**CREDENTIALS, "extraEnv.DFE_RECEIVER_S3_SECRET": "env-secret"}
        )
        shown = client.get(f"{RECEIVER_BASE}/values", headers=admin_headers).json()["values"]
        auth = shown["config"]["server"]["auth"]

        resp = _put_receiver(
            client,
            admin_headers,
            {
                "config.server.auth.mode": "header",
                "config.server.auth.bearer.tokens": auth["bearer"]["tokens"],
                "config.server.auth.accepted_headers": auth["accepted_headers"],
                "config.server.auth.header_values": auth["header_values"],
                "extraEnv.DFE_RECEIVER_S3_SECRET": shown["extraEnv"]["DFE_RECEIVER_S3_SECRET"],
            },
        )
        assert resp.status_code == 200, resp.text

        stored = gc.get("helmvars", "dfe-receiver-default-values")
        assert stored["config"]["server"]["auth"] == {
            "mode": "header",
            "bearer": {"tokens": ["tok-1", "tok-2"]},
            "accepted_headers": [{"name": "x-api-key", "values": ["hv-1"]}],
            "header_values": ["legacy-1"],
        }
        assert stored["extraEnv"]["DFE_RECEIVER_S3_SECRET"] == "env-secret"
        assert contract.REDACTED not in json.dumps(stored)

    def test_a_masked_entry_added_to_a_list_keeps_the_stored_ones_and_takes_the_new(
        self, client, app, admin_headers, tmp_path
    ):
        gc = _wire(app, tmp_path)
        _deploy_receiver(client, admin_headers)
        _put_receiver(client, admin_headers, CREDENTIALS)
        path = "config.server.auth.bearer.tokens"
        resp = _put_receiver(
            client, admin_headers, {path: [contract.REDACTED, contract.REDACTED, "tok-3"]}
        )
        assert resp.status_code == 200, resp.text
        stored = gc.get("helmvars", "dfe-receiver-default-values")
        assert stored["config"]["server"]["auth"]["bearer"]["tokens"] == ["tok-1", "tok-2", "tok-3"]

    def test_the_mask_with_nothing_stored_behind_it_is_refused(
        self, client, app, admin_headers, tmp_path
    ):
        # Writing it would put the placeholder itself in as the credential.
        gc = _wire(app, tmp_path)
        _deploy_receiver(client, admin_headers)
        before = gc.head_revision()
        resp = _put_receiver(
            client, admin_headers, {"config.kafka.sasl.password": contract.REDACTED}
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "masked_value"
        assert gc.head_revision() == before

    def test_the_helm_vars_route_keeps_a_credential_written_back_masked(
        self, client, app, admin_headers, tmp_path
    ):
        gc = _wire(app, tmp_path)
        _deploy_receiver(client, admin_headers)
        _put_receiver(client, admin_headers, CREDENTIALS)
        resp = client.put(
            "/api/v1/helm/files/dfe-receiver-default-values/vars/config.server.auth.bearer.tokens",
            json={"value": [contract.REDACTED, contract.REDACTED]},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        stored = gc.get("helmvars", "dfe-receiver-default-values")
        assert stored["config"]["server"]["auth"]["bearer"]["tokens"] == ["tok-1", "tok-2"]

    def test_the_helm_vars_route_refuses_the_mask_with_nothing_behind_it(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        _deploy_receiver(client, admin_headers)
        resp = client.put(
            "/api/v1/helm/files/dfe-receiver-default-values/vars/config.kafka.sasl.password",
            json={"value": contract.REDACTED},
            headers=admin_headers,
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "masked_value"

    def test_the_stored_overlay_still_holds_what_was_written(
        self, client, app, admin_headers, tmp_path
    ):
        # Masking is on the way out: the app still needs the real tokens.
        gc = _wire(app, tmp_path)
        _deploy_receiver(client, admin_headers)
        _put_receiver(client, admin_headers, CREDENTIALS)
        doc = gc.get("helmvars", "dfe-receiver-default-values")
        assert doc["config"]["server"]["auth"]["bearer"]["tokens"] == ["tok-1", "tok-2"]
