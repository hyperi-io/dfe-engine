#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_e2e_server.py
#  Purpose:      e2e-server-only API group (seed-admin) is absent outside that mode
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries
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
_ARTIFACT = "seed-artefact"


@pytest.fixture(autouse=True)
def _hermetic_break_glass(monkeypatch):
    """Do not inherit DFE_AUTH_LOCAL_ADMIN_* from a developer .env."""
    monkeypatch.delenv("DFE_AUTH_LOCAL_ADMIN_NAME", raising=False)
    monkeypatch.delenv("DFE_AUTH_LOCAL_ADMIN_PASSWORD", raising=False)


def _settings(
    tmp_path: Path,
    *,
    e2e_server: bool,
    env: str = "test",
    gitops: bool = False,
    deployment_target: str = "unknown",
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
        deployment=DeploymentSettings(target=deployment_target),
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


def _admin(client: TestClient) -> dict[str, str]:
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "changeme"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _get(client: TestClient, path: str, headers: dict[str, str]) -> dict | list:
    resp = client.get(path, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _instances(client: TestClient, headers: dict[str, str], service: str) -> list[str]:
    listed = _get(client, _APPS, headers)
    by_service = {entry["service"]: entry for entry in listed}
    return by_service[service]["instances"]


class TestSeedSourceWithTransform:
    """The Sources/<source> page: a source, its bound apps, and compiled routing."""

    def test_the_seeded_source_and_its_apps_are_readable_over_the_api(self, appmgmt_client):
        _seed(appmgmt_client, "seed_source_with_transform")
        headers = _admin(appmgmt_client)

        source = _get(appmgmt_client, f"{_SOURCES}/{_SOURCE}", headers)
        assert source["source"] == _SOURCE
        version = source["versions"][source["current"]]
        assert version["match"]["value"] == _SOURCE
        # The fetcher half of the page, which the source model carries itself.
        assert version["fetcher"]["source_type"] == "http_json"

        assert _instances(appmgmt_client, headers, _VRL) == [_SOURCE]
        assert _instances(appmgmt_client, headers, "dfe-fetcher") == [_SOURCE]

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
        assert [rule["source"] for rule in found["deployed"]["source_rules"]] == [_SOURCE]

    def test_the_loader_routing_is_synced_too(self, appmgmt_client):
        _seed(appmgmt_client, "seed_source_with_transform")
        headers = _admin(appmgmt_client)

        found = _get(appmgmt_client, f"{_APPS}/dfe-loader/default/routing", headers)
        assert (found["drift"], found["absent"]) == (False, False)

    def test_seeding_twice_duplicates_nothing(self, appmgmt_client):
        _seed(appmgmt_client, "seed_source_with_transform")
        _seed(appmgmt_client, "seed_source_with_transform")
        headers = _admin(appmgmt_client)

        listed = _get(appmgmt_client, _SOURCES, headers)
        assert [s["name"] for s in listed["items"]] == [_SOURCE]
        assert _instances(appmgmt_client, headers, _VRL) == [_SOURCE]
        files = _get(appmgmt_client, f"{_APPS}/{_VRL}/{_SOURCE}/files/transforms", headers)
        assert [f["name"] for f in files] == [f"{_SOURCE}.vrl"]

    def test_reset_all_removes_the_source_and_its_instances(self, appmgmt_client):
        _seed(appmgmt_client, "seed_source_with_transform")
        _seed(appmgmt_client, "reset_all")
        headers = _admin(appmgmt_client)

        assert _get(appmgmt_client, _SOURCES, headers)["items"] == []
        assert _instances(appmgmt_client, headers, _VRL) == []
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
