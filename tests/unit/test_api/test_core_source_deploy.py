#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_core_source_deploy.py
#  Purpose:      A core source's deploy is refused before ClickHouse is touched
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Deploying an engine-owned source is refused before any DDL is applied.

A deploy records the version it applied, and recording it on a core source is
refused. The route used to apply the DDL, fence the table and ensure the topics
first, so the 409 arrived after the table existed. The shipped landing definition
carries no schema, which hides this behind a 404; these tests give it one.
"""

import copy

import pytest
from fastapi.testclient import TestClient

from dfe_engine.source.core_sources import LANDING_SOURCE_FILE, SOURCES_SUBDIR
from dfe_engine.yaml_utils import yaml_dump
from tests.support.core_sources import LANDING_DEFINITION


@pytest.fixture
def landing_with_schema(api_settings) -> None:
    """The landing definition with a schema on its current version, seeded at startup."""
    definition = copy.deepcopy(LANDING_DEFINITION)
    current = definition["current"]
    definition["versions"][current]["schema"] = {"meta_schema": "meta/beats/filebeat"}
    path = f"{api_settings.schemas.schemas_dir}/{SOURCES_SUBDIR}/{LANDING_SOURCE_FILE}"
    yaml_dump(definition, path)


@pytest.fixture
def core_client(landing_with_schema, client: TestClient) -> TestClient:
    return client


def _no_clickhouse(*args, **kwargs):
    raise AssertionError("the deploy reached ClickHouse")


class TestCoreSourceDeploy:
    def test_the_deploy_is_refused_before_clickhouse(
        self, core_client: TestClient, admin_headers: dict, monkeypatch
    ):
        monkeypatch.setattr(
            "dfe_engine.clickhouse.clickhouse_manager.ClickHouseManager.get_instance",
            _no_clickhouse,
        )

        resp = core_client.post("/api/v1/sources/main/deploy", headers=admin_headers)

        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "conflict"
        assert "main" in resp.json()["message"]

    def test_a_dry_run_goes_on_to_the_build(self, core_client: TestClient, admin_headers: dict):
        # The tmp schemas tree holds no meta schema, so a dry run that got past the
        # core check stops at the build instead.
        resp = core_client.post(
            "/api/v1/sources/main/deploy", params={"dry_run": "true"}, headers=admin_headers
        )

        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "build_error"
