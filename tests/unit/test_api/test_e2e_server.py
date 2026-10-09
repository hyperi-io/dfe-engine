#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_e2e_server.py
#  Purpose:      e2e-server-only API group (seed-admin) is absent outside that mode
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries
from dfe_engine.api.e2e.seed.apps import TRANSFORM_ENGINES
from dfe_engine.auth.bootstrap import admin_account_password
from dfe_engine.settings import (
    APISettings,
    AuthSettings,
    ClickHouseSettings,
    DeploymentSettings,
    DFESettings,
    GitopsSettings,
    HuntsSettings,
    LocalAuthSettings,
    SchemasSettings,
    SecretsSettings,
    ServicesSettings,
    SourceSettings,
)

_SEED = "/api/e2e/seed-static"
_STATUS = "/api/e2e/status"

_APPS = "/api/v1/apps"
_LIB = "/api/v1/library"
_SOURCES = "/api/v1/sources"

_VRL = "dfe-transform-vrl"
_SOURCE = "seedsource"
_FETCHED = "seedfetch"
_ARTIFACT = "seed-artefact"


@pytest.fixture(autouse=True)
def _hermetic_break_glass(monkeypatch):
    """Do not inherit admin from a developer .env."""


def _settings(
    tmp_path: Path,
    *,
    e2e_server: bool,
    env: str = "test",
    gitops: bool = False,
    deployment_target: str = "unknown",
    profile: str = "",
) -> DFESettings:
    """Settings for an e2e-server process.

    ``gitops`` stands a real local deploy repo up the way ``make e2e-server`` does
    when it is configured for one: the app-management seeds write into it, and the
    source registry backs onto its ``config/sources``.
    """
    for sub in ("sources", "services", "rules", "hunts", "auth", "schemas", "secrets"):
        (tmp_path / sub).mkdir()
    return DFESettings(
        env=env,
        e2e_server=e2e_server,
        config_dir=str(tmp_path),
        clickhouse=ClickHouseSettings(bootstrap_tables=False),
        deployment=DeploymentSettings(target=deployment_target, profile=profile),
        gitops=GitopsSettings(
            enabled=gitops,
            local_path=str(tmp_path / "deploy") if gitops else "",
            push=False,
        ),
        source=SourceSettings(sources_dir=str(tmp_path / "sources")),
        services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
        schemas=SchemasSettings(schemas_dir=str(tmp_path / "schemas")),
        hunts=HuntsSettings(rules_dir=str(tmp_path / "rules"), hunt_dir=str(tmp_path / "hunts")),
        auth=AuthSettings(
            enabled=True,
            auth_dir=str(tmp_path / "auth"),
            local=LocalAuthSettings(enabled=True, admin_password="changeme"),
        ),
        secrets=SecretsSettings(provider="file", path=str(tmp_path / "secrets")),
        api=APISettings(jwt_secret="test-secret-key-for-unit-tests-hmac32"),
    )


def test_e2e_routes_absent_when_flag_off(tmp_path):
    app = create_app(settings=_settings(tmp_path, e2e_server=False))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            assert client.get(_STATUS).status_code == 404
            assert client.post(_SEED, json={"script": "seed_dfe_admin_user"}).status_code == 404
    finally:
        _registries.clear()


def test_e2e_openapi_absent_when_flag_off(tmp_path):
    app = create_app(settings=_settings(tmp_path, e2e_server=False))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            spec = client.get("/openapi.json").json()
            assert _SEED not in spec["paths"]
            assert _STATUS not in spec["paths"]
            assert "E2E" not in {t["name"] for t in spec.get("tags", [])}
            assert client.get("/openapi.e2e.json").status_code == 404
            docs = client.get("/docs").text
            assert "openapi.e2e.json" not in docs
    finally:
        _registries.clear()


def test_build_e2e_spec_documents_helpers_only():
    from dfe_engine.api.e2e_docs import build_e2e_spec

    spec = build_e2e_spec(version="dev")
    assert set(spec["paths"]) == {_STATUS, _SEED}
    assert spec["paths"][_STATUS]["get"]["tags"] == ["E2E"]
    assert spec["paths"][_SEED]["post"]["tags"] == ["E2E"]
    assert spec["paths"][_SEED]["post"].get("security") == []
    e2e_tags = [t for t in spec.get("tags", []) if t["name"] == "E2E"]
    assert len(e2e_tags) == 1
    assert "Playwright" in e2e_tags[0]["description"]
    assert "/api/v1/auth/login" not in spec["paths"]


def test_e2e_openapi_group_when_flag_on(tmp_path):
    app = create_app(settings=_settings(tmp_path, e2e_server=True))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            api_spec = client.get("/openapi.json").json()
            e2e_spec = client.get("/openapi.e2e.json").json()
            docs = client.get("/docs").text

        assert _STATUS not in api_spec["paths"]
        assert _SEED not in api_spec["paths"]
        assert "E2E" not in {t["name"] for t in api_spec.get("tags", [])}

        assert _STATUS in e2e_spec["paths"]
        assert _SEED in e2e_spec["paths"]
        assert e2e_spec["paths"][_STATUS]["get"]["tags"] == ["E2E"]
        assert e2e_spec["paths"][_SEED]["post"]["tags"] == ["E2E"]
        assert e2e_spec["paths"][_SEED]["post"].get("security") == []
        e2e_tags = [t for t in e2e_spec.get("tags", []) if t["name"] == "E2E"]
        assert len(e2e_tags) == 1
        assert "Playwright" in e2e_tags[0]["description"]
        assert "/api/v1/auth/login" not in e2e_spec["paths"]

        assert "openapi.e2e.json" in docs
        assert "StandaloneLayout" in docs
        assert '"name": "API"' in docs or '"name":"API"' in docs
        assert '"name": "E2E"' in docs or '"name":"E2E"' in docs
    finally:
        _registries.clear()


def test_e2e_status_and_seed_dfe_admin_when_flag_on(tmp_path):
    app = create_app(settings=_settings(tmp_path, e2e_server=True))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            status = client.get(_STATUS)
            assert status.status_code == 200
            assert status.json() == {"enabled": True}

            # The seeder rides on the startup bootstrap: dfe-admins already
            # exists, and the break-glass admin is a SEPARATE account it never
            # touches. Nothing is torn down first.
            assert app.state.group_store.get("dfe-admins") is not None
            assert app.state.account_store.get("admin") is not None

            resp = client.post(_SEED, json={"script": "seed_dfe_admin_user"})
            assert resp.status_code == 200
            body = resp.json()
            assert body["success"] is True
            assert "password" not in body

            login = client.post(
                "/api/v1/auth/login",
                json={"username": "dfe_admin", "password": "changeme"},
            )
            assert login.status_code == 200
            assert "admin" in login.json()["roles"]
    finally:
        _registries.clear()


def test_e2e_seed_dfe_admin_resets_existing_password(tmp_path):
    app = create_app(settings=_settings(tmp_path, e2e_server=True))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            first = client.post(_SEED, json={"script": "seed_dfe_admin_user"})
            assert first.status_code == 200
            assert first.json()["success"] is True

            app.state.account_store.reset_password("dfe_admin", "first-pass")
            second = client.post(_SEED, json={"script": "seed_dfe_admin_user"})
            assert second.status_code == 200
            assert second.json()["success"] is True

            assert (
                client.post(
                    "/api/v1/auth/login",
                    json={"username": "dfe_admin", "password": "first-pass"},
                ).status_code
                == 401
            )
            assert (
                client.post(
                    "/api/v1/auth/login",
                    json={"username": "dfe_admin", "password": "changeme"},
                ).status_code
                == 200
            )
    finally:
        _registries.clear()


@pytest.fixture
def appmgmt_client(tmp_path):
    """An e2e-server process with a real deploy repo and a Kubernetes deploy target.

    Everything the app-management seeds produce is read back over the product API in
    the same process, so the assertions exercise the routers a Playwright spec drives
    rather than the files underneath them.
    """
    app = create_app(
        settings=_settings(tmp_path, e2e_server=True, gitops=True, deployment_target="kubernetes")
    )
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client
    finally:
        _registries.clear()


def _seed(client: TestClient, script: str) -> None:
    resp = client.post(_SEED, json={"script": script})
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True


# The shipped default these settings leave the admin on, issued with a forced change.
_ISSUED_ADMIN_PASSWORD = admin_account_password()
_ADMIN_OWN_PASSWORD = f"e2e-admin-{secrets.token_urlsafe(12)}"


def _admin(client: TestClient) -> dict[str, str]:
    """Admin headers, after the forced change a fresh deployment's admin is due.

    The change ends the session that made it, so the admin signs in again after it.
    """
    for password in (_ISSUED_ADMIN_PASSWORD, _ADMIN_OWN_PASSWORD):
        login = client.post("/api/v1/auth/login", json={"username": "admin", "password": password})
        if login.status_code == 200:
            break
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    if login.json()["password_change_required"]:
        changed = client.post(
            "/api/v1/auth/accounts/reset-password",
            json={"current_password": password, "new_password": _ADMIN_OWN_PASSWORD},
            headers=headers,
        )
        assert changed.status_code == 200, changed.text
        login = client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": _ADMIN_OWN_PASSWORD}
        )
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    return headers


def _get(client: TestClient, path: str, headers: dict[str, str]) -> dict | list:
    resp = client.get(path, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _instances(client: TestClient, headers: dict[str, str], service: str) -> list[str]:
    listed = _get(client, _APPS, headers)
    by_service = {entry["service"]: entry for entry in listed}
    return by_service[service]["instances"]


def _operator_sources(client: TestClient, headers: dict[str, str]) -> list[str]:
    """Seeded source names, without the landing source the engine always owns."""
    listed = _get(client, _SOURCES, headers)
    return [s["name"] for s in listed["items"] if s["resource_type"] != "core"]


class TestSeedRefusalIsLegible:
    """A refused seed and a crashed seed must not answer the same way (#452)."""

    def test_a_profile_that_deploys_no_fetcher_answers_422_on_the_real_path(self, tmp_path):
        """The refusal #452 was filed for, driven rather than faked.

        `docker-slim` runs the core data path alone, so the catalogue does not
        offer dfe-fetcher there and `source/flow.py` refuses the fetcher-based
        source the seed writes.
        """
        app = create_app(
            settings=_settings(tmp_path, e2e_server=True, gitops=True, profile="docker-slim")
        )
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                resp = client.post(_SEED, json={"script": "seed_source_with_transform"})
        finally:
            _registries.clear()

        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert body["code"] == "validation_error"
        assert "dfe-fetcher" in body["message"]
        assert "docker-slim" in body["message"]

    def test_a_refused_seed_answers_422_with_the_reason(self, appmgmt_client, monkeypatch):
        from dfe_engine.api.e2e.seed import Seed
        from dfe_engine.source.registry import SourceValidationError

        reason = "source 'seedfetch' needs dfe-fetcher, and this profile does not deploy it"

        def _refuse(self, script):
            raise SourceValidationError(reason)

        monkeypatch.setattr(Seed, "seed_static", _refuse)

        resp = appmgmt_client.post(_SEED, json={"script": "seed_source_with_transform"})

        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert body["code"] == "validation_error"
        assert body["message"] == reason


class TestSeedSourceWithTransform:
    """The Sources/<source> page: a source, its bound apps, and compiled routing."""

    def test_the_seeded_source_and_its_apps_are_readable_over_the_api(self, appmgmt_client):
        _seed(appmgmt_client, "seed_source_with_transform")
        headers = _admin(appmgmt_client)

        source = _get(appmgmt_client, f"{_SOURCES}/{_SOURCE}", headers)
        assert source["source"] == _SOURCE
        version = source["versions"][source["current"]]
        assert version["match"]["value"] == _SOURCE
        assert version["fetcher"] is None
        assert source["origin"] == "receiver"

        # The fetcher-based sibling carries the stanza its instance was compiled from.
        fetched = _get(appmgmt_client, f"{_SOURCES}/{_FETCHED}", headers)
        assert fetched["origin"] == "fetcher"
        assert fetched["deployed_version"] == "1.0.0"
        assert fetched["versions"]["1.0.0"]["fetcher"]["source_type"] == "crates_io"

        assert _instances(appmgmt_client, headers, _VRL) == [_SOURCE]
        assert _instances(appmgmt_client, headers, "dfe-fetcher") == [_FETCHED]
        stanza = _get(appmgmt_client, f"{_APPS}/dfe-fetcher/{_FETCHED}/routing", headers)
        assert stanza["drift"] is False
        assert stanza["deployed"]["sources"]["crates_io"]["topic"] == _FETCHED

        summary = _get(appmgmt_client, f"{_APPS}/{_VRL}/{_SOURCE}", headers)
        assert summary["telemetry_name"] == f"{_VRL}-{_SOURCE}"
        assert summary["multiplicity"] == "per_config"

    def test_the_transform_carries_a_program_the_editor_can_open(self, appmgmt_client):
        _seed(appmgmt_client, "seed_source_with_transform")
        headers = _admin(appmgmt_client)

        listed = _get(appmgmt_client, f"{_APPS}/{_VRL}/{_SOURCE}/files/transforms", headers)
        assert [f["name"] for f in listed] == [f"{_SOURCE}.vrl"]

        found = _get(
            appmgmt_client, f"{_APPS}/{_VRL}/{_SOURCE}/files/transforms/{_SOURCE}.vrl", headers
        )
        assert found["language"] == "vrl"
        assert f'.dfe_source = "{_SOURCE}"' in found["content"]

    def test_the_receiver_routing_is_compiled_from_the_seeded_source(self, appmgmt_client):
        _seed(appmgmt_client, "seed_source_with_transform")
        headers = _admin(appmgmt_client)

        found = _get(appmgmt_client, f"{_APPS}/dfe-receiver/default/routing", headers)
        assert (found["drift"], found["absent"]) == (False, False)
        # The fetcher-based source is routed on the _source label its fetcher stamps.
        assert [rule["source"] for rule in found["deployed"]["routing"]["source_rules"]] == [
            _FETCHED,
            _SOURCE,
        ]

    def test_the_loader_routing_is_synced_too(self, appmgmt_client):
        _seed(appmgmt_client, "seed_source_with_transform")
        headers = _admin(appmgmt_client)

        found = _get(appmgmt_client, f"{_APPS}/dfe-loader/default/routing", headers)
        assert (found["drift"], found["absent"]) == (False, False)

    def test_seeding_twice_duplicates_nothing(self, appmgmt_client):
        _seed(appmgmt_client, "seed_source_with_transform")
        _seed(appmgmt_client, "seed_source_with_transform")
        headers = _admin(appmgmt_client)

        assert _operator_sources(appmgmt_client, headers) == [_FETCHED, _SOURCE]
        assert _instances(appmgmt_client, headers, _VRL) == [_SOURCE]
        assert _instances(appmgmt_client, headers, "dfe-fetcher") == [_FETCHED]
        files = _get(appmgmt_client, f"{_APPS}/{_VRL}/{_SOURCE}/files/transforms", headers)
        assert [f["name"] for f in files] == [f"{_SOURCE}.vrl"]

    def test_reset_all_removes_the_source_and_its_instances(self, appmgmt_client):
        _seed(appmgmt_client, "seed_source_with_transform")
        _seed(appmgmt_client, "reset_all")
        headers = _admin(appmgmt_client)

        assert _operator_sources(appmgmt_client, headers) == []
        assert _instances(appmgmt_client, headers, _VRL) == []


class TestSeedThreeTransforms:
    """One source per transform app, which the single-vrl seeds cannot express."""

    def test_each_transform_app_gets_its_own_source_and_instance(self, appmgmt_client):
        _seed(appmgmt_client, "seed_three_transforms")
        headers = _admin(appmgmt_client)

        for engine, service, _variant in TRANSFORM_ENGINES:
            name = f"filebeat{engine}"
            source = _get(appmgmt_client, f"{_SOURCES}/{name}", headers)
            version = source["versions"][source["current"]]
            assert version["transform"]["engine"] == engine
            assert _instances(appmgmt_client, headers, service) == [name]

    def test_the_file_driven_apps_take_no_variant_and_elastic_does(self, appmgmt_client):
        _seed(appmgmt_client, "seed_three_transforms")
        headers = _admin(appmgmt_client)

        def variant_of(engine: str) -> str | None:
            source = _get(appmgmt_client, f"{_SOURCES}/filebeat{engine}", headers)
            return source["versions"][source["current"]]["transform"]["variant"]

        # dfe-transform-elastic reads no authored files and selects a compiled-in
        # program by name, so it is the only one carrying a variant.
        assert variant_of("vrl") is None
        assert variant_of("vector") is None
        assert variant_of("elastic") == "filebeat.cisco_ios.default"

    def test_seeding_twice_duplicates_nothing(self, appmgmt_client):
        _seed(appmgmt_client, "seed_three_transforms")
        _seed(appmgmt_client, "seed_three_transforms")
        headers = _admin(appmgmt_client)

        assert sorted(_operator_sources(appmgmt_client, headers)) == [
            "filebeatelastic",
            "filebeatvector",
            "filebeatvrl",
        ]

    def test_reset_all_removes_all_three(self, appmgmt_client):
        _seed(appmgmt_client, "seed_three_transforms")
        _seed(appmgmt_client, "reset_all")
        headers = _admin(appmgmt_client)

        assert _operator_sources(appmgmt_client, headers) == []
        for _engine, service, _variant in TRANSFORM_ENGINES:
            assert _instances(appmgmt_client, headers, service) == []
        assert _instances(appmgmt_client, headers, "dfe-receiver") == []
        assert appmgmt_client.get(f"{_SOURCES}/{_SOURCE}", headers=headers).status_code == 404


class TestSeedLibraryArtefact:
    """The Library page, plus the per-file link and drift surfaces it feeds."""

    def test_the_artefact_carries_two_versions_a_tag_and_labels(self, appmgmt_client):
        _seed(appmgmt_client, "seed_library_artefact")
        headers = _admin(appmgmt_client)

        assert [a["name"] for a in _get(appmgmt_client, _LIB, headers)] == [_ARTIFACT]

        found = _get(appmgmt_client, f"{_LIB}/{_ARTIFACT}", headers)
        assert found["kind"] == "vrl"
        assert found["versions"] == [1, 2]
        assert found["current"] == 2
        assert found["tags"] == {"stable": 1}
        assert found["group"] == "seed"
        assert found["labels"] == {"team": "platform", "tier": "gold"}

        versions = _get(appmgmt_client, f"{_LIB}/{_ARTIFACT}/versions", headers)
        assert [v["version"] for v in versions] == [1, 2]
        assert versions[0]["digest"] != versions[1]["digest"]

    def test_the_version_content_reads_back_verbatim(self, appmgmt_client):
        _seed(appmgmt_client, "seed_library_artefact")
        headers = _admin(appmgmt_client)

        found = _get(appmgmt_client, f"{_LIB}/{_ARTIFACT}/versions/2", headers)
        assert ".dfe_enriched = true" in found["content"]

    def test_the_link_and_its_usage_are_both_visible(self, appmgmt_client):
        _seed(appmgmt_client, "seed_library_artefact")
        headers = _admin(appmgmt_client)

        usage = _get(appmgmt_client, f"{_LIB}/{_ARTIFACT}/usage", headers)
        assert len(usage) == 1
        assert (usage[0]["service"], usage[0]["instance"]) == (_VRL, _SOURCE)
        assert usage[0]["name"] == "seed_library.vrl"

        links = _get(appmgmt_client, f"{_APPS}/{_VRL}/{_SOURCE}/files/transforms/links", headers)
        assert len(links) == 1
        # Pinned behind the current version, so the page has a real outdated signal
        # without the resolved content having drifted from what it was linked to.
        assert (links[0]["version"], links[0]["available_version"]) == (1, 2)
        assert (links[0]["outdated"], links[0]["drift"], links[0]["missing"]) == (
            True,
            False,
            False,
        )

    def test_the_linked_content_lands_in_the_file_set(self, appmgmt_client):
        _seed(appmgmt_client, "seed_library_artefact")
        headers = _admin(appmgmt_client)

        listed = _get(appmgmt_client, f"{_APPS}/{_VRL}/{_SOURCE}/files/transforms", headers)
        assert sorted(f["name"] for f in listed) == ["seed_library.vrl", f"{_SOURCE}.vrl"]

        found = _get(
            appmgmt_client,
            f"{_APPS}/{_VRL}/{_SOURCE}/files/transforms/seed_library.vrl",
            headers,
        )
        assert ".dfe_artefact = 1" in found["content"]

    def test_seeding_twice_adds_no_version_and_no_second_link(self, appmgmt_client):
        _seed(appmgmt_client, "seed_library_artefact")
        _seed(appmgmt_client, "seed_library_artefact")
        headers = _admin(appmgmt_client)

        found = _get(appmgmt_client, f"{_LIB}/{_ARTIFACT}", headers)
        assert found["versions"] == [1, 2]
        assert found["current"] == 2
        assert found["tags"] == {"stable": 1}
        assert len(_get(appmgmt_client, f"{_LIB}/{_ARTIFACT}/usage", headers)) == 1
        assert [a["name"] for a in _get(appmgmt_client, _LIB, headers)] == [_ARTIFACT]

    def test_reset_all_removes_the_artefact(self, appmgmt_client):
        _seed(appmgmt_client, "seed_library_artefact")
        _seed(appmgmt_client, "reset_all")
        headers = _admin(appmgmt_client)

        assert _get(appmgmt_client, _LIB, headers) == []
        assert appmgmt_client.get(f"{_LIB}/{_ARTIFACT}", headers=headers).status_code == 404


class TestSeedAppScalingState:
    """The Components page: pools whose dials are set rather than chart defaults."""

    def test_the_pools_report_the_seeded_dials(self, appmgmt_client):
        _seed(appmgmt_client, "seed_app_scaling_state")
        headers = _admin(appmgmt_client)

        for service in ("dfe-receiver", "dfe-loader"):
            found = _get(appmgmt_client, f"{_APPS}/{service}/default/scaling", headers)
            assert found["supported"] is True
            assert found["deploy_target"] == "kubernetes"
            assert (found["min_replicas"], found["max_replicas"]) == (2, 12)
            assert found["keda_enabled"] is True
            assert (found["cpu_request"], found["memory_request"]) == ("250m", "512Mi")
            assert (found["cpu_limit"], found["memory_limit"]) == ("1", "1Gi")

    def test_seeding_twice_leaves_the_same_dials(self, appmgmt_client):
        _seed(appmgmt_client, "seed_app_scaling_state")
        _seed(appmgmt_client, "seed_app_scaling_state")
        headers = _admin(appmgmt_client)

        found = _get(appmgmt_client, f"{_APPS}/dfe-receiver/default/scaling", headers)
        assert (found["min_replicas"], found["max_replicas"]) == (2, 12)
        assert _instances(appmgmt_client, headers, "dfe-receiver") == ["default"]

    def test_reset_all_removes_the_pools(self, appmgmt_client):
        _seed(appmgmt_client, "seed_app_scaling_state")
        _seed(appmgmt_client, "reset_all")
        headers = _admin(appmgmt_client)

        assert _instances(appmgmt_client, headers, "dfe-receiver") == []
        assert (
            appmgmt_client.get(f"{_APPS}/dfe-receiver/default/scaling", headers=headers).status_code
            == 404
        )

    def test_reset_all_still_leaves_the_break_glass_admin(self, appmgmt_client):
        _seed(appmgmt_client, "seed_app_scaling_state")
        _seed(appmgmt_client, "reset_all")
        assert _admin(appmgmt_client)

    def test_reset_all_leaves_the_admin_due_its_forced_change(self, appmgmt_client):
        """A fresh deployment's admin: the onboarding root logs in, then must change it."""
        _admin(appmgmt_client)
        _seed(appmgmt_client, "reset_all")

        login = appmgmt_client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": _ISSUED_ADMIN_PASSWORD},
        )

        assert login.status_code == 200, login.text
        assert login.json()["password_change_required"] is True

    def test_setup_complete_leaves_the_admin_on_its_own_password(self, appmgmt_client):
        _seed(appmgmt_client, "reset_all")
        _seed(appmgmt_client, "seed_setup_complete")

        assert appmgmt_client.app.state.account_store.get("admin").password_change_required is False


_POOLS = ("dfe-receiver", "dfe-loader")
_JOB_OVERLAY = (
    "# Enables {svc} for this deployment (seeded at bootstrap).\n"
    "deploy:\n  service: {svc}\n  instance: default\n"
)


def _stood_up_as_on_kubernetes(deploy: Path) -> None:
    """Commit what the Kubernetes setup job writes: the pools' overlays and the marker."""
    from dulwich import porcelain

    porcelain.init(str(deploy))
    (deploy / "values").mkdir()
    paths = []
    for svc in _POOLS:
        target = deploy / "values" / f"{svc}-default-values.yaml"
        target.write_text(_JOB_OVERLAY.format(svc=svc), encoding="utf-8")
        paths.append(str(target))
    marker = deploy / ".seeded-apps"
    marker.write_text("".join(f"{svc}\n" for svc in _POOLS), encoding="utf-8")
    paths.append(str(marker))
    porcelain.add(str(deploy), paths=paths)
    porcelain.commit(str(deploy), message=b"seed", author=b"j <j@j>", committer=b"j <j@j>")


def _deploy_commits(client: TestClient) -> int:
    from dulwich.repo import Repo

    with Repo(str(client.app.state.gitcrud.repo_path)) as repo:
        return sum(1 for _ in repo.get_walker())


@pytest.fixture
def k8s_client(tmp_path):
    """An e2e-server process over a deploy repo the Kubernetes setup job stood up."""
    _stood_up_as_on_kubernetes(tmp_path / "deploy")
    app = create_app(
        settings=_settings(tmp_path, e2e_server=True, gitops=True, deployment_target="kubernetes")
    )
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client
    finally:
        _registries.clear()


class TestResetKeepsTheDeployersPools:
    """On Kubernetes the pools are the deployment's own data path, and Argo prunes a deleted one."""

    def test_the_pools_stay_deployed_with_the_seeded_dials_gone(self, k8s_client):
        _seed(k8s_client, "seed_app_scaling_state")
        _seed(k8s_client, "reset_all")
        headers = _admin(k8s_client)

        for service in _POOLS:
            assert _instances(k8s_client, headers, service) == ["default"]
            found = _get(k8s_client, f"{_APPS}/{service}/default/scaling", headers)
            assert (found["min_replicas"], found["max_replicas"]) == (None, None)
            assert found["cpu_request"] is None

    def test_the_pools_keep_the_deployers_identity_and_nothing_else(self, k8s_client):
        _seed(k8s_client, "seed_source_with_transform")
        _seed(k8s_client, "seed_app_scaling_state")
        _seed(k8s_client, "reset_all")

        gc = k8s_client.app.state.gitcrud
        doc = gc.get("helmvars", "dfe-receiver-default-values")
        assert doc["deploy"] == {"service": "dfe-receiver", "instance": "default"}
        assert "keda" not in doc
        assert "resources" not in doc
        # The job wrote no OTel name, so the reset does not invent one.
        assert "otelServiceName" not in doc
        assert "otel" not in doc

    def test_the_routing_is_recompiled_without_the_seeded_sources(self, k8s_client):
        _seed(k8s_client, "seed_source_with_transform")
        headers = _admin(k8s_client)
        seeded = _get(k8s_client, f"{_APPS}/dfe-receiver/default/routing", headers)
        assert _SOURCE in [r["source"] for r in seeded["deployed"]["routing"]["source_rules"]]

        _seed(k8s_client, "reset_all")
        headers = _admin(k8s_client)

        for service in _POOLS:
            found = _get(k8s_client, f"{_APPS}/{service}/default/routing", headers)
            assert (found["drift"], found["absent"]) == (False, False)
        rules = _get(k8s_client, f"{_APPS}/dfe-receiver/default/routing", headers)
        names = [r["source"] for r in rules["deployed"]["routing"]["source_rules"]]
        assert _SOURCE not in names
        assert _FETCHED not in names

    def test_a_pool_a_run_removed_is_deployed_again(self, k8s_client):
        gc = k8s_client.app.state.gitcrud
        gc.delete("helmvars", "dfe-loader-default-values", "test", message="test: removed")

        _seed(k8s_client, "reset_all")
        headers = _admin(k8s_client)

        assert _instances(k8s_client, headers, "dfe-loader") == ["default"]

    def test_the_per_source_instances_still_go(self, k8s_client):
        _seed(k8s_client, "seed_three_transforms")
        _seed(k8s_client, "reset_all")
        headers = _admin(k8s_client)

        for _engine, service, _variant in TRANSFORM_ENGINES:
            assert _instances(k8s_client, headers, service) == []


class TestOneCommitPerSeed:
    """A write per commit and push costs a remote forge round trip for every one of them."""

    @pytest.mark.parametrize(
        "script",
        [
            "seed_app_scaling_state",
            "seed_library_artefact",
            "seed_source_with_transform",
            "seed_three_transforms",
            "seed_setup_complete",
        ],
    )
    def test_a_seed_and_the_reset_after_it_are_one_commit_each(self, k8s_client, script):
        before = _deploy_commits(k8s_client)
        _seed(k8s_client, script)
        seeded = _deploy_commits(k8s_client)
        _seed(k8s_client, "reset_all")

        assert seeded - before == 1
        assert _deploy_commits(k8s_client) - seeded == 1

    def test_a_seed_with_nothing_left_to_write_commits_nothing(self, k8s_client):
        _seed(k8s_client, "seed_app_scaling_state")
        before = _deploy_commits(k8s_client)

        _seed(k8s_client, "seed_app_scaling_state")

        assert _deploy_commits(k8s_client) == before

    def test_a_refused_seed_leaves_nothing_behind(self, tmp_path):
        """docker-slim refuses the fetcher source after the receiver source was written."""
        app = create_app(
            settings=_settings(tmp_path, e2e_server=True, gitops=True, profile="docker-slim")
        )
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                before = _deploy_commits(client)
                resp = client.post(_SEED, json={"script": "seed_source_with_transform"})
                sources = app.state.gitcrud.list("sources")
                after = _deploy_commits(client)
        finally:
            _registries.clear()

        assert resp.status_code == 422, resp.text
        assert _SOURCE not in sources
        assert after == before


class TestSeedingLeavesTheEventLoopFree:
    _WAIT_SECONDS = 10.0

    def test_a_seed_in_flight_does_not_hold_up_other_requests(self, appmgmt_client, monkeypatch):
        """On the event loop, a parked seed would leave the status probe unanswered."""
        import threading

        from dfe_engine.api.e2e.seed import Seed

        started, release = threading.Event(), threading.Event()
        real_seed_static = Seed.seed_static
        wait = self._WAIT_SECONDS

        def parked(seeder, script):
            started.set()
            assert release.wait(timeout=wait * 3)
            return real_seed_static(seeder, script)

        monkeypatch.setattr(Seed, "seed_static", parked)
        seeded: list[int] = []
        probed: list[int] = []

        def seed() -> None:
            resp = appmgmt_client.post(_SEED, json={"script": "seed_app_scaling_state"})
            seeded.append(resp.status_code)

        def probe() -> None:
            probed.append(appmgmt_client.get(_STATUS).status_code)

        seeding = threading.Thread(target=seed, daemon=True)
        seeding.start()
        assert started.wait(timeout=wait)
        prober = threading.Thread(target=probe, daemon=True)
        prober.start()
        prober.join(timeout=wait)
        try:
            assert probed == [200]
            assert seeded == []
        finally:
            release.set()
            seeding.join(timeout=wait * 3)
            prober.join(timeout=wait)

        assert seeded == [200]


_BACKING = "/api/v1/backing-services"
_CH_OVERLAY = "clickhouse-cluster"


class TestResetClearsRunState:
    """A reset has to take the substrate overlay with it, or specs inherit it.

    The live failure this pins: one spec raised a backing-service node count, the
    next reset left it raised, and that spec then failed against its predecessor's
    leftovers on a workspace it believed was clean.
    """

    @staticmethod
    def _raise_node_count(client: TestClient, headers: dict[str, str]) -> None:
        resp = client.put(
            f"{_BACKING}/overlays/{_CH_OVERLAY}/vars/clickhouse.replicas",
            json={"value": 5},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text

    def test_a_raised_node_count_does_not_survive_reset_all(self, appmgmt_client):
        _seed(appmgmt_client, "seed_app_scaling_state")
        headers = _admin(appmgmt_client)
        self._raise_node_count(appmgmt_client, headers)

        # It really is declared before the reset, or the test proves nothing.
        declared = {s["service"]: s for s in _get(appmgmt_client, _BACKING, headers)}
        assert declared["clickhouse"]["replicas"]["value"] == 5
        assert declared["clickhouse"]["replicas"]["source"] is not None

        _seed(appmgmt_client, "reset_all")

        headers = _admin(appmgmt_client)
        after = {s["service"]: s for s in _get(appmgmt_client, _BACKING, headers)}
        for service in after.values():
            for field in ("mode", "storage_model", "replicas", "storage_size", "storage_class"):
                assert service[field]["value"] is None, f"{service['service']}.{field}"
                assert service[field]["source"] is None
        assert _get(appmgmt_client, f"{_BACKING}/overlays", headers) == []

    def test_the_auto_merge_flag_does_not_survive_reset_all(self, appmgmt_client):
        """Absent reads as off, which is the posture a fresh deployment starts in."""
        headers = _admin(appmgmt_client)
        enabled = appmgmt_client.put(
            "/api/v1/gitops/auto-merge", json={"enabled": True}, headers=headers
        )
        assert enabled.status_code == 200, enabled.text
        assert _get(appmgmt_client, "/api/v1/gitops/auto-merge", headers)["stored"] is True

        _seed(appmgmt_client, "reset_all")

        headers = _admin(appmgmt_client)
        assert _get(appmgmt_client, "/api/v1/gitops/auto-merge", headers)["stored"] is False

    def test_the_shipped_governance_library_survives_reset_all(self, appmgmt_client):
        """Actions and policies are product configuration, not run state."""
        gc = appmgmt_client.app.state.gitcrud
        actions = set(gc.list("actions"))
        policies = set(gc.list("policies"))
        assert actions, "the startup seed should have populated the action library"
        assert policies, "the startup seed should have populated the policy library"

        _seed(appmgmt_client, "reset_all")

        assert set(gc.list("actions")) == actions
        assert set(gc.list("policies")) == policies

    def test_reset_all_still_leaves_the_break_glass_durable(self, appmgmt_client):
        """Clearing run state must not disturb the account copy the reset re-mirrors."""
        _seed(appmgmt_client, "seed_setup_complete")
        _seed(appmgmt_client, "reset_all")

        app = appmgmt_client.app
        live = app.state.account_store.get("admin")
        stored = app.state.gitcrud.get("accounts", "admin")
        assert stored["password_hash"] == live.password_hash

    def test_clearing_run_state_is_a_no_op_without_a_deploy_repo(self, tmp_path):
        app = create_app(settings=_settings(tmp_path, e2e_server=True))
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                assert app.state.gitcrud is None
                _seed(client, "reset_all")
                assert not (tmp_path / "deploy").exists()
        finally:
            _registries.clear()


def _setup_status(client: TestClient) -> dict:
    resp = client.get("/api/v1/auth/setup-status")
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestBreakGlassDurability:
    """Seeding has to leave the break-glass admin durable in the deploy repo.

    ``setup-status`` reports the account as durable only once the repo's copy
    matches the live one, so a seeder that wrote only the live store left every
    gitops-enabled Playwright run reading an undurable break-glass account.
    """

    @staticmethod
    def _client(tmp_path):
        return create_app(settings=_settings(tmp_path, e2e_server=True, gitops=True))

    def test_seeding_leaves_the_break_glass_merged_and_setup_complete(self, tmp_path):
        app = self._client(tmp_path)
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                _seed(client, "seed_setup_complete")

                status = _setup_status(client)
                assert status["break_glass"]["committed"] is True
                assert status["break_glass"]["merged"] is True
                assert status["break_glass"]["pending"] is None
                assert status["initial_setup"]["complete"] is True
                assert status["initial_setup"]["current_step"] is None
        finally:
            _registries.clear()

    def test_the_committed_hash_is_the_live_one_not_a_second_derivation(self, tmp_path):
        """bcrypt re-salts, so the hash has to be mirrored rather than re-derived."""
        app = self._client(tmp_path)
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                _seed(client, "seed_setup_complete")
                live = app.state.account_store.get("admin")
                stored = app.state.gitcrud.get("accounts", "admin")
                assert stored["password_hash"] == live.password_hash
        finally:
            _registries.clear()

    def test_the_seeded_break_glass_password_still_logs_in(self, tmp_path):
        app = self._client(tmp_path)
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                _seed(client, "seed_setup_complete")
                login = client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "already_reset"},
                )
                assert login.status_code == 200, login.text
        finally:
            _registries.clear()

    def test_reseeding_after_reset_all_is_merged_again(self, tmp_path):
        """The Playwright arrangement: reset_all then seed, over and over."""
        app = self._client(tmp_path)
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                for _ in range(2):
                    _seed(client, "reset_all")
                    _seed(client, "seed_setup_complete")
                    status = _setup_status(client)
                    assert status["break_glass"]["merged"] is True
                    assert status["initial_setup"]["complete"] is True

                live = app.state.account_store.get("admin")
                stored = app.state.gitcrud.get("accounts", "admin")
                assert stored["password_hash"] == live.password_hash
        finally:
            _registries.clear()

    def test_only_the_break_glass_account_reaches_the_deploy_repo(self, tmp_path):
        """A regular seeded account is durable in its own store; git is the exception."""
        app = self._client(tmp_path)
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                _seed(client, "seed_setup_complete")
                _seed(client, "seed_dfe_analyst_user")
                committed = set(app.state.gitcrud.list("accounts"))
                assert committed == {"admin"}
        finally:
            _registries.clear()

    def test_gitops_off_seeds_with_no_deploy_repo_at_all(self, tmp_path):
        """The mirror only happens on the gitops path; without one nothing changes."""
        app = create_app(settings=_settings(tmp_path, e2e_server=True))
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                assert app.state.gitcrud is None
                _seed(client, "seed_setup_complete")
                assert not (tmp_path / "deploy").exists()

                status = _setup_status(client)
                # No deploy repo means the live store IS the durable store, so the
                # durability question does not arise.
                assert status["break_glass"]["enabled"] is False
                assert status["break_glass"]["merged"] is True
                assert status["initial_setup"]["complete"] is True

                login = client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "already_reset"},
                )
                assert login.status_code == 200, login.text
        finally:
            _registries.clear()


class TestBreakGlassReviewPosture:
    """A posture that refuses direct-to-main reports pending rather than pretending.

    Not reachable from `make e2e-server`, whose DFE_ENV=test is a dev posture and
    so always commits straight to main -- the seed gate requires that env. Built
    directly here so the honest-pending branch is still pinned.
    """

    @staticmethod
    def _seeder(tmp_path):
        from dfe_engine.api.e2e.seed import Seed
        from dfe_engine.auth.bootstrap import bootstrap_auth
        from dfe_engine.gitcrud import GitCrud, default_registry
        from dfe_engine.gitops.repo import GitopsRepo
        from dfe_engine.orgs.registry import OrgRegistry

        gc = GitCrud(
            GitopsRepo(local_path=str(tmp_path / "deploy"), push=False), default_registry()
        )
        accounts, groups, *_ = bootstrap_auth(tmp_path / "config" / "auth", gitcrud=gc)
        # e2e_server=False because settings REFUSE to mount the seed routes in a
        # production posture at all -- which is the same reason this branch cannot
        # be reached through the endpoint. Only the routing posture is wanted here.
        settings = _settings(tmp_path, e2e_server=False, env="production", gitops=True)
        settings.gitops.mode = "team"
        # The gate is the seeder's own env; the ROUTING posture is the settings'.
        seeder = Seed(
            account_store=accounts,
            group_store=groups,
            org_registry=OrgRegistry(tmp_path / "orgs"),
            env="test",
            gitcrud=gc,
            settings=settings,
            forge=None,
        )
        return seeder, gc, accounts, settings

    def test_a_review_posture_commits_to_a_branch_and_reports_unmerged(self, tmp_path, monkeypatch):
        from dfe_engine.auth.account_durability import steady_state

        monkeypatch.setenv("DFE_ENV", "test")
        seeder, gc, accounts, settings = self._seeder(tmp_path)

        assert seeder.seed_static("seed_setup_complete") is True

        state = steady_state(
            gc, accounts, "admin", environment=settings.env, mode=settings.gitops.mode
        )
        # Committed (on a review branch, so the change is not lost), but the
        # tracked branch still carries the old hash, so it is honestly unmerged.
        assert state.committed is True
        assert state.merged is False
        assert gc.get("accounts", "admin")["password_hash"] != accounts.get("admin").password_hash


def test_an_app_seed_without_a_deploy_repo_fails_loudly(tmp_path):
    """A misconfigured e2e-server must not report a seed that wrote nothing."""
    app = create_app(settings=_settings(tmp_path, e2e_server=True))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post(_SEED, json={"script": "seed_source_with_transform"})
            assert resp.status_code == 500
    finally:
        _registries.clear()


def test_e2e_seed_does_not_disturb_the_bootstrap_break_glass_admin(tmp_path):
    """The seeders add e2e accounts; startup bootstrap still owns ``admin``.

    `make e2e-server` must come up with exactly what `dfe-engine run` seeds --
    default groups plus the break-glass admin -- and a seed call must not
    rewrite that credential out from under a test.
    """
    app = create_app(settings=_settings(tmp_path, e2e_server=True))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            assert client.post(_SEED, json={"script": "seed_dfe_admin_user"}).status_code == 200
            assert (
                client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "changeme"},
                ).status_code
                == 200
            )
    finally:
        _registries.clear()
