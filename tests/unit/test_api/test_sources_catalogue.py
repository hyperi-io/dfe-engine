#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_sources_catalogue.py
#  Purpose:      Listing the dfe-schemas source catalogue, and creating a source from an entry
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The catalogue routes, over the installed dfe-schemas catalogue and a mounted elastic contract."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dfe_engine.appmgmt import contract
from dfe_engine.source import catalogue as sc
from tests.unit.test_api.sources_catalogue_cases import (
    CREATE_AGAINST_THE_CONTRACT_CASES,
    CREATE_FROM_CATALOGUE_CASES,
    CREATE_FROM_CATALOGUE_REJECT_CASES,
    LIST_AGAINST_THE_CONTRACT_CASES,
    CreateAgainstTheContractCase,
    CreateFromCatalogueCase,
    CreateFromCatalogueRejectCase,
    ListAgainstTheContractCase,
)
from tests.unit.test_source.source_catalogue_cases import ELASTIC_CONTRACT, NO_CONTRACT

CATALOGUE = "/api/v1/sources/catalogue"
FROM_CATALOGUE = "/api/v1/sources/from-catalogue"


def _row(*, client: TestClient, headers: dict[str, str], name: str) -> dict[str, Any]:
    """The listed row for one entry, found by searching its name."""
    resp = client.get(CATALOGUE, headers=headers, params={"search": name})
    return next(row for row in resp.json()["items"] if row["name"] == name)


def _stored(*, client: TestClient, headers: dict[str, str], name: str) -> dict[str, Any]:
    """The origin, transform and schema a created source was stored with."""
    stored = client.get(f"/api/v1/sources/{name}", headers=headers).json()
    version = stored["versions"][stored["current"]]
    fetcher = version["fetcher"]
    return {
        "fetcher": fetcher["source_type"] if fetcher else None,
        "match": stored["match"],
        "meta_schema": version["schema"]["meta_schema"],
        "origin": stored["origin"],
        "variant": stored["transform"]["variant"],
    }


@pytest.fixture(autouse=True)
def _no_contract(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A deployment that mounts no app contracts, and none held after the test."""
    monkeypatch.setenv(contract.CONTRACT_DIR_ENV, str(NO_CONTRACT))
    contract.reload_contracts()
    yield
    contract.reload_contracts()


@pytest.fixture
def elastic_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment that mounts the trimmed dfe-transform-elastic contract."""
    monkeypatch.setenv(contract.CONTRACT_DIR_ENV, str(ELASTIC_CONTRACT))
    contract.reload_contracts()


class TestListCatalogue:
    def test_an_entry_carries_what_the_console_offers(
        self, admin_headers: dict[str, str], client: TestClient
    ):
        assert _row(client=client, headers=admin_headers, name="okta") == {
            "name": "okta",
            "display_name": "okta.system",
            "description": "The system data stream of the Elastic okta integration.",
            "package": "okta",
            "data_stream": "system",
            "dataset": "okta.system",
            "intakes": ["beats", "fetcher"],
            "default_intake": "fetcher",
            "framing": None,
            "beats": {"module": "okta", "fileset": "system"},
            "has_schema": True,
            "engine": "elastic",
            "variant": "filebeat.okta.default",
            "variants": ["filebeat.okta.default"],
            "availability": "unknown",
            "availability_reason": "no 'dfe-transform-elastic' contract is mounted, so its "
            "variants are not known",
        }

    @pytest.mark.parametrize(
        "case",
        LIST_AGAINST_THE_CONTRACT_CASES,
        ids=[case["id"] for case in LIST_AGAINST_THE_CONTRACT_CASES],
    )
    def test_against_the_contract(
        self,
        admin_headers: dict[str, str],
        client: TestClient,
        elastic_contract: None,
        case: ListAgainstTheContractCase,
    ):
        row = _row(client=client, headers=admin_headers, name=case["entry"])
        assert row["availability"] == case["expected_availability"]

    def test_pages(self, admin_headers: dict[str, str], client: TestClient):
        data = client.get(CATALOGUE, headers=admin_headers, params={"per_page": 5}).json()
        assert (data["total"], len(data["items"])) == (len(sc.source_catalogue().entries), 5)

    def test_intake_filter(self, admin_headers: dict[str, str], client: TestClient):
        params = {"intake": "receiver", "per_page": -1}
        listed = client.get(CATALOGUE, headers=admin_headers, params=params).json()["items"]
        entries = sc.source_catalogue().entries
        receivers = [name for name, entry in entries.items() if "receiver" in entry.intakes]
        assert [row["name"] for row in listed] == receivers

    def test_unknown_intake(self, admin_headers: dict[str, str], client: TestClient):
        resp = client.get(CATALOGUE, headers=admin_headers, params={"intake": "carrier"})
        assert (resp.status_code, resp.json()["message"]) == (
            422,
            "Unknown intake 'carrier'. Must be: beats, fetcher, receiver",
        )

    def test_needs_source_read(self, client: TestClient):
        assert client.get(CATALOGUE).status_code == 401

    def test_a_viewer_may_list(self, client: TestClient, viewer_headers: dict[str, str]):
        assert client.get(CATALOGUE, headers=viewer_headers).status_code == 200


class TestCreateSourceFromCatalogue:
    @pytest.mark.parametrize(
        "case",
        CREATE_FROM_CATALOGUE_CASES,
        ids=[case["id"] for case in CREATE_FROM_CATALOGUE_CASES],
    )
    def test_matches_expected(
        self, admin_headers: dict[str, str], client: TestClient, case: CreateFromCatalogueCase
    ):
        resp = client.post(
            f"{FROM_CATALOGUE}/{case['entry']}", headers=admin_headers, json=case["body"]
        )
        assert resp.status_code == 201, resp.text
        stored = _stored(client=client, headers=admin_headers, name=resp.json()["source"])
        assert stored == case["expected_stored"]

    @pytest.mark.parametrize(
        "case",
        CREATE_AGAINST_THE_CONTRACT_CASES,
        ids=[case["id"] for case in CREATE_AGAINST_THE_CONTRACT_CASES],
    )
    def test_against_the_contract(
        self,
        admin_headers: dict[str, str],
        client: TestClient,
        elastic_contract: None,
        case: CreateAgainstTheContractCase,
    ):
        resp = client.post(
            f"{FROM_CATALOGUE}/{case['entry']}", headers=admin_headers, json=case["body"]
        )
        assert resp.status_code == case["expected_status"], resp.text

    def test_missing_variant(
        self, admin_headers: dict[str, str], client: TestClient, elastic_contract: None
    ):
        resp = client.post(
            f"{FROM_CATALOGUE}/o365-metrics-entra-alerts", headers=admin_headers, json={}
        )
        saved = client.get("/api/v1/sources/o365-metrics-entra-alerts", headers=admin_headers)
        assert (resp.status_code, resp.json(), saved.status_code) == (
            409,
            {
                "code": "missing_variant",
                "message": "the deployed 'dfe-transform-elastic' does not ship transform variant "
                "'filebeat.o365_metrics_entra_alerts.default'",
                "errors": [],
                "context": {
                    "transform": "dfe-transform-elastic",
                    "variant": "filebeat.o365_metrics_entra_alerts.default",
                },
            },
            404,
        )

    @pytest.mark.parametrize(
        "case",
        CREATE_FROM_CATALOGUE_REJECT_CASES,
        ids=[case["id"] for case in CREATE_FROM_CATALOGUE_REJECT_CASES],
    )
    def test_rejects(
        self, admin_headers: dict[str, str], client: TestClient, case: CreateFromCatalogueRejectCase
    ):
        resp = client.post(
            f"{FROM_CATALOGUE}/{case['entry']}", headers=admin_headers, json=case["body"]
        )
        assert (resp.status_code, resp.json()) == (422, case["expected_response"])

    def test_twice(self, admin_headers: dict[str, str], client: TestClient):
        first = client.post(f"{FROM_CATALOGUE}/cisco-asa", headers=admin_headers, json={})
        again = client.post(f"{FROM_CATALOGUE}/cisco-asa", headers=admin_headers, json={})
        assert (first.status_code, again.status_code) == (201, 409)

    def test_needs_source_write(self, client: TestClient):
        assert client.post(f"{FROM_CATALOGUE}/cisco-asa", json={}).status_code == 401

    def test_a_viewer_may_not_create(self, client: TestClient, viewer_headers: dict[str, str]):
        resp = client.post(f"{FROM_CATALOGUE}/cisco-asa", headers=viewer_headers, json={})
        saved = client.get("/api/v1/sources/cisco-asa", headers=viewer_headers)
        assert (resp.status_code, saved.status_code) == (403, 404)
