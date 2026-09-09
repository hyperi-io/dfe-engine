#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_sources_catalogue.py
#  Purpose:      Listing a shipped source catalogue, and creating a source from an entry
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The catalogue routes, over the vendored copy of the shipped catalogue."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dfe_engine.source import catalogue as sc

SHIPPED = Path(__file__).parents[2] / "fixtures" / "catalogue" / "sources.yaml"


@pytest.fixture
def mounted(monkeypatch):
    """A deployment with the catalogue mounted, and none held after the test."""
    monkeypatch.setenv(sc.CATALOGUE_FILE_ENV, str(SHIPPED))
    sc.reload_source_catalogue()
    yield
    monkeypatch.delenv(sc.CATALOGUE_FILE_ENV, raising=False)
    sc.reload_source_catalogue()


@pytest.fixture
def unmounted(monkeypatch, tmp_path):
    """A deployment that mounts no catalogue at all."""
    monkeypatch.delenv(sc.CATALOGUE_FILE_ENV, raising=False)
    monkeypatch.setattr(sc, "DEFAULT_CATALOGUE_PATH", tmp_path / "absent.yaml")
    sc.reload_source_catalogue()
    yield
    sc.reload_source_catalogue()


class TestListCatalogue:
    def test_a_deployment_with_no_catalogue_lists_nothing(
        self, client: TestClient, admin_headers: dict, unmounted
    ):
        resp = client.get("/api/v1/sources/catalogue", headers=admin_headers)

        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_the_whole_catalogue_paginates(self, client: TestClient, admin_headers: dict, mounted):
        resp = client.get("/api/v1/sources/catalogue?per_page=5", headers=admin_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1069
        assert len(data["items"]) == 5

    def test_an_entry_carries_what_the_console_needs_to_offer_it(
        self, client: TestClient, admin_headers: dict, mounted
    ):
        resp = client.get("/api/v1/sources/catalogue?search=okta", headers=admin_headers)

        rows = {row["name"]: row for row in resp.json()["items"]}
        assert rows["okta"] == {
            "name": "okta",
            "package": "okta",
            "data_stream": "system",
            "dataset": "okta.system",
            "intakes": ["beats", "fetcher"],
            "framing": None,
            "transforms": ["default"],
            "beats": {"module": "okta", "fileset": "system"},
            "source": "okta",
        }

    def test_an_entry_whose_name_is_not_a_legal_source_name_says_so(
        self, client: TestClient, admin_headers: dict, mounted
    ):
        resp = client.get("/api/v1/sources/catalogue?search=1password", headers=admin_headers)

        rows = resp.json()["items"]
        assert rows
        assert all(row["source"] == "" for row in rows)

    def test_the_intake_filter_only_returns_entries_that_arrive_that_way(
        self, client: TestClient, admin_headers: dict, mounted
    ):
        resp = client.get("/api/v1/sources/catalogue?intake=receiver", headers=admin_headers)

        data = resp.json()
        assert data["total"] == 135
        assert all("receiver" in row["intakes"] for row in data["items"])

    def test_an_unknown_intake_is_refused(self, client: TestClient, admin_headers: dict, mounted):
        resp = client.get("/api/v1/sources/catalogue?intake=carrier", headers=admin_headers)

        assert resp.status_code == 422
        assert "beats, fetcher, receiver" in resp.json()["message"]

    def test_reading_the_catalogue_needs_source_read(self, client: TestClient):
        assert client.get("/api/v1/sources/catalogue").status_code == 401


class TestCreateFromCatalogue:
    def test_an_entry_becomes_a_real_source(self, client: TestClient, admin_headers: dict, mounted):
        resp = client.post(
            "/api/v1/sources/from-catalogue/okta",
            json={"intake": "beats"},
            headers=admin_headers,
        )

        assert resp.status_code == 201, resp.text
        assert resp.json()["source"] == "okta"

        stored = client.get("/api/v1/sources/okta", headers=admin_headers).json()
        assert stored["match"] == {
            "field": "data_stream.dataset",
            "operator": "equals",
            "value": "okta.system",
        }
        assert stored["transform"] == {
            "engine": "elastic",
            "variant": "filebeat.okta.default",
            "config_file": None,
            "env": {},
            "files": [],
        }

    def test_a_pulled_entry_becomes_a_fetcher_based_source(
        self, client: TestClient, admin_headers: dict, mounted
    ):
        resp = client.post(
            "/api/v1/sources/from-catalogue/aws_cloudtrail",
            json={"intake": "fetcher"},
            headers=admin_headers,
        )

        assert resp.status_code == 201, resp.text
        stored = client.get("/api/v1/sources/aws-cloudtrail", headers=admin_headers).json()
        assert stored["origin"] == "fetcher"
        assert stored["match"] is None
        version = stored["versions"][stored["current"]]
        assert version["fetcher"]["source_type"] == "aws"

    def test_an_entry_whose_name_is_not_a_legal_source_name_takes_one(
        self, client: TestClient, admin_headers: dict, mounted
    ):
        refused = client.post(
            "/api/v1/sources/from-catalogue/1password_audit_events",
            json={"intake": "fetcher"},
            headers=admin_headers,
        )
        assert refused.status_code == 422
        assert "DNS-1123" in refused.json()["message"]

        named = client.post(
            "/api/v1/sources/from-catalogue/1password_audit_events",
            json={"intake": "fetcher", "name": "onepassword-audit"},
            headers=admin_headers,
        )
        assert named.status_code == 201, named.text
        assert named.json()["source"] == "onepassword-audit"

    def test_an_intake_the_entry_does_not_arrive_by_is_refused(
        self, client: TestClient, admin_headers: dict, mounted
    ):
        resp = client.post(
            "/api/v1/sources/from-catalogue/okta",
            json={"intake": "receiver"},
            headers=admin_headers,
        )

        assert resp.status_code == 422
        assert resp.json()["code"] == "catalogue_error"

    def test_an_entry_that_is_not_in_the_catalogue_is_refused(
        self, client: TestClient, admin_headers: dict, mounted
    ):
        resp = client.post(
            "/api/v1/sources/from-catalogue/not-a-source",
            json={"intake": "beats"},
            headers=admin_headers,
        )

        assert resp.status_code == 422
        assert "not-a-source" in resp.json()["message"]

    def test_creating_twice_conflicts_like_any_other_create(
        self, client: TestClient, admin_headers: dict, mounted
    ):
        body = {"intake": "beats"}
        first = client.post("/api/v1/sources/from-catalogue/okta", json=body, headers=admin_headers)
        assert first.status_code == 201

        again = client.post("/api/v1/sources/from-catalogue/okta", json=body, headers=admin_headers)
        assert again.status_code == 409

    def test_a_deployment_with_no_catalogue_says_so(
        self, client: TestClient, admin_headers: dict, unmounted
    ):
        resp = client.post(
            "/api/v1/sources/from-catalogue/okta",
            json={"intake": "beats"},
            headers=admin_headers,
        )

        assert resp.status_code == 503
        assert resp.json()["code"] == "not_configured"

    def test_creating_from_the_catalogue_needs_source_write(self, client: TestClient):
        resp = client.post("/api/v1/sources/from-catalogue/okta", json={"intake": "beats"})

        assert resp.status_code == 401
