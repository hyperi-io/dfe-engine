#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_sources_catalogue.py
#  Purpose:      Listing the dfe-schemas source catalogue, and creating sources from entries
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The catalogue routes, over the installed dfe-schemas catalogue and a mounted elastic contract."""

from collections.abc import Iterator
from pathlib import Path
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
from tests.unit.test_source.factories import CATALOGUE_FIXTURES
from tests.unit.test_source.source_catalogue_cases import ELASTIC_CONTRACT, NO_CONTRACT

CATALOGUE = "/api/v1/sources/catalogue"
CATALOGUE_TAGS = "/api/v1/sources/catalogue-tags"
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
        "from_catalogue_intake": stored["from_catalogue_intake"],
        "from_catalogue_item": stored["from_catalogue_item"],
        "origin": stored["origin"],
        "variant": stored["transform"]["variant"],
    }


def _create(client: TestClient, headers: dict[str, str], entry: str, body: dict[str, Any]) -> Any:
    """Create one catalogue entry, with the entry name beside the caller's fields."""
    return client.post(
        FROM_CATALOGUE, headers=headers, json={"entries": [{"entry": entry, **body}]}
    )


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
            "runnable": True,
            "runnable_reason": "",
            "runnable_intakes": ["beats", "fetcher"],
        }

    @pytest.mark.parametrize(
        ("default", "runnable", "intakes"),
        [("bus", True, ["beats", "fetcher"]), ("direct", False, [])],
    )
    def test_an_entry_says_whether_this_deployment_can_run_it(
        self, admin_headers, client, app, default, runnable, intakes
    ):
        app.state.settings.transport.bus_present = default == "bus"
        app.state.settings.transport.default = default

        row = _row(client=client, headers=admin_headers, name="okta")

        assert (row["runnable"], row["runnable_intakes"]) == (runnable, intakes)
        assert ("per-source app" in row["runnable_reason"]) is (not runnable)

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

    def test_tags_keep_entries_in_those_directories(
        self, admin_headers: dict[str, str], client: TestClient, tmp_path: Path, monkeypatch
    ):
        holding = tmp_path / "holding"
        (holding / "aws").mkdir(parents=True)
        entries = CATALOGUE_FIXTURES / "entries"
        (holding / "okta.yaml").write_bytes((entries / "okta.yaml").read_bytes())
        (holding / "aws" / "aws-cloudtrail.yaml").write_bytes(
            (entries / "aws-cloudtrail.yaml").read_bytes()
        )
        monkeypatch.setattr(
            sc, "source_catalogue", lambda: sc.load_source_catalogue(directory=holding)
        )

        def listed(tags: list[str] | None = None) -> list[str]:
            params: dict[str, Any] = {"per_page": -1}
            if tags is not None:
                params["tags"] = tags
            resp = client.get(CATALOGUE, headers=admin_headers, params=params)
            assert resp.status_code == 200, resp.text
            return [row["name"] for row in resp.json()["items"]]

        assert listed() == ["aws-cloudtrail", "okta"]
        assert listed(["aws"]) == ["aws-cloudtrail"]
        assert listed(["UNTAGGED"]) == ["okta"]
        assert listed(["aws", "untagged"]) == ["aws-cloudtrail", "okta"]
        assert listed(["nope"]) == []

    @pytest.mark.parametrize(
        ("params", "expected"),
        [
            ({}, ["cisco-asa"]),
            ({"engine": "elastic"}, []),
            ({"engine": "vector"}, ["cisco-asa"]),
            ({"engine": "VECTOR"}, ["cisco-asa"]),
            ({"engine": "nobody"}, []),
            ({"include_stored": True}, ["cisco-asa", "okta"]),
            ({"engine": "elastic", "include_stored": True}, ["okta"]),
            ({"engine": "Elastic", "include_stored": True}, ["okta"]),
            ({"engine": "vector", "include_stored": True}, ["cisco-asa"]),
            ({"engine": "nobody", "include_stored": True}, []),
        ],
        ids=[
            "default-drops-stored",
            "engine-drops-stored-by-default",
            "engine-keeps-that-engine",
            "engine-ignores-case",
            "unknown-engine-lists-nothing",
            "include-stored-keeps-stored",
            "engine-with-include-stored",
            "engine-case-with-include-stored",
            "other-engine-with-include-stored",
            "unknown-engine-with-include-stored",
        ],
    )
    def test_engine_and_include_stored(
        self,
        admin_headers: dict[str, str],
        client: TestClient,
        tmp_path: Path,
        monkeypatch,
        params: dict[str, Any],
        expected: list[str],
    ):
        holding = tmp_path / "holding"
        holding.mkdir()
        entries = CATALOGUE_FIXTURES / "entries"
        (holding / "okta.yaml").write_bytes((entries / "okta.yaml").read_bytes())
        other = (entries / "cisco-asa.yaml").read_text(encoding="utf-8")
        (holding / "cisco-asa.yaml").write_text(
            other.replace("engine: elastic", "engine: vector"), encoding="utf-8"
        )
        monkeypatch.setattr(
            sc, "source_catalogue", lambda: sc.load_source_catalogue(directory=holding)
        )
        created = _create(client, admin_headers, "okta", {"intake": "beats"})
        assert created.status_code == 201, created.text

        resp = client.get(CATALOGUE, headers=admin_headers, params={**params, "per_page": -1})

        assert resp.status_code == 200, resp.text
        assert [row["name"] for row in resp.json()["items"]] == expected
        assert resp.json()["total"] == len(expected)

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

    def test_an_entry_already_stored_is_left_out(
        self, admin_headers: dict[str, str], client: TestClient
    ):
        before = client.get(CATALOGUE, headers=admin_headers, params={"per_page": 1}).json()[
            "total"
        ]
        created = _create(client, admin_headers, "okta", {"intake": "beats"})
        assert created.status_code == 201, created.text
        listed = client.get(CATALOGUE, headers=admin_headers, params={"search": "okta"})
        again = client.get(CATALOGUE, headers=admin_headers, params={"per_page": 1}).json()["total"]
        assert "okta" not in {row["name"] for row in listed.json()["items"]}
        assert again == before - 1

    def test_a_renamed_catalogue_source_still_hides_its_entry(
        self, admin_headers: dict[str, str], client: TestClient
    ):
        created = _create(client, admin_headers, "cisco-asa", {"name": "edge-firewall"})
        assert created.status_code == 201, created.text
        listed = client.get(CATALOGUE, headers=admin_headers, params={"search": "cisco-asa"})
        assert "cisco-asa" not in {row["name"] for row in listed.json()["items"]}

    def test_a_hand_written_source_leaves_its_catalogue_entry_listed(
        self, admin_headers: dict[str, str], client: TestClient
    ):
        written = client.post(
            "/api/v1/sources",
            headers=admin_headers,
            json={"source": "okta", "match": {"field": "tags.collector.type", "value": "okta"}},
        )
        assert written.status_code == 201, written.text
        listed = client.get(CATALOGUE, headers=admin_headers, params={"search": "okta"})
        assert "okta" in {row["name"] for row in listed.json()["items"]}


class TestListCatalogueTags:
    def test_a_tag_is_a_holding_directory(
        self, admin_headers: dict[str, str], client: TestClient, tmp_path: Path, monkeypatch
    ):
        holding = tmp_path / "holding"
        holding.mkdir()
        for tag in ("aws", "abnormal", "active-mq"):
            (holding / tag).mkdir()
        (holding / "okta.yaml").write_text("not a tag\n", encoding="utf-8")
        (holding / ".hidden").mkdir()
        monkeypatch.setattr(sc, "catalogue_tags_directory", lambda: holding)

        listed = client.get(CATALOGUE_TAGS, headers=admin_headers, params={"per_page": -1}).json()

        assert [(row["id"], row["name"], row["description"]) for row in listed["items"]] == [
            ("abnormal", "abnormal", ""),
            ("active-mq", "active-mq", ""),
            ("aws", "aws", ""),
            ("untagged", "untagged", ""),
        ]

    def test_name_keeps_a_tag_that_starts_with_it(
        self, admin_headers: dict[str, str], client: TestClient, tmp_path: Path, monkeypatch
    ):
        holding = tmp_path / "holding"
        holding.mkdir()
        for tag in ("aws", "abnormal", "active-mq"):
            (holding / tag).mkdir()
        (holding / "okta.yaml").write_text("not a tag\n", encoding="utf-8")
        monkeypatch.setattr(sc, "catalogue_tags_directory", lambda: holding)

        listed = client.get(
            CATALOGUE_TAGS, headers=admin_headers, params={"name": "ACT", "per_page": -1}
        ).json()
        middle = client.get(
            CATALOGUE_TAGS, headers=admin_headers, params={"name": "mq", "per_page": -1}
        ).json()
        loose = client.get(
            CATALOGUE_TAGS, headers=admin_headers, params={"name": "un", "per_page": -1}
        ).json()

        assert [row["id"] for row in listed["items"]] == ["active-mq"]
        assert middle["items"] == []
        assert [row["id"] for row in loose["items"]] == ["untagged"]

    def test_the_dictionary_supplies_the_display_name(
        self, admin_headers: dict[str, str], client: TestClient, tmp_path: Path, monkeypatch
    ):
        holding = tmp_path / "catalogue"
        (holding / "aws").mkdir(parents=True)
        registry = tmp_path / "registries"
        registry.mkdir()
        (registry / "catalogue-tags.yaml").write_text(
            "aws:\n  name: Amazon Web Services\n  description: Logs and metrics from AWS services.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(sc, "catalogue_tags_directory", lambda: holding)

        listed = client.get(CATALOGUE_TAGS, headers=admin_headers, params={"per_page": -1}).json()
        by_name = client.get(
            CATALOGUE_TAGS, headers=admin_headers, params={"name": "Amazon", "per_page": -1}
        ).json()

        assert listed["items"] == [
            {
                "id": "aws",
                "name": "Amazon Web Services",
                "description": "Logs and metrics from AWS services.",
            }
        ]
        assert [row["id"] for row in by_name["items"]] == ["aws"]

    def test_needs_source_read(self, client: TestClient):
        assert client.get(CATALOGUE_TAGS).status_code == 401

    def test_a_viewer_may_list(self, client: TestClient, viewer_headers: dict[str, str]):
        assert client.get(CATALOGUE_TAGS, headers=viewer_headers).status_code == 200


class TestCreateSourceFromCatalogue:
    @pytest.mark.parametrize(
        "case",
        CREATE_FROM_CATALOGUE_CASES,
        ids=[case["id"] for case in CREATE_FROM_CATALOGUE_CASES],
    )
    def test_matches_expected(
        self, admin_headers: dict[str, str], client: TestClient, case: CreateFromCatalogueCase
    ):
        resp = _create(client, admin_headers, case["entry"], case["body"])
        assert resp.status_code == 201, resp.text
        name = str(case["body"].get("name") or case["entry"])
        assert resp.json()["failed"] == []
        assert resp.json()["created"][0]["source"] == name
        stored = _stored(client=client, headers=admin_headers, name=name)
        assert stored == case["expected_stored"]

    def test_several_entries_become_sources_together(
        self, admin_headers: dict[str, str], client: TestClient
    ):
        resp = client.post(
            FROM_CATALOGUE,
            headers=admin_headers,
            json={
                "entries": [
                    {"entry": "okta", "intake": "beats"},
                    {"entry": "cisco-asa"},
                ]
            },
        )
        assert resp.status_code == 201, resp.text
        assert [row["source"] for row in resp.json()["created"]] == ["okta", "cisco-asa"]
        assert resp.json()["failed"] == []
        assert client.get("/api/v1/sources/okta", headers=admin_headers).status_code == 200
        assert client.get("/api/v1/sources/cisco-asa", headers=admin_headers).status_code == 200

    def test_more_than_fifty_entries_are_refused(
        self, admin_headers: dict[str, str], client: TestClient
    ):
        resp = client.post(
            FROM_CATALOGUE,
            headers=admin_headers,
            json={"entries": [{"entry": "okta", "intake": "beats"}] * 51},
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "validation_error"
        assert resp.json()["message"]
        assert "created" not in resp.json()
        assert client.get("/api/v1/sources/okta", headers=admin_headers).status_code == 404

    def test_one_refusal_leaves_the_other_entries_stored(
        self, admin_headers: dict[str, str], client: TestClient
    ):
        resp = client.post(
            FROM_CATALOGUE,
            headers=admin_headers,
            json={
                "entries": [
                    {"entry": "okta", "intake": "beats"},
                    {"entry": "not-a-source", "intake": "beats"},
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [row["source"] for row in body["created"]] == ["okta"]
        assert body["failed"][0]["entry"] == "not-a-source"
        assert body["failed"][0]["code"] == "catalogue_error"
        assert client.get("/api/v1/sources/okta", headers=admin_headers).status_code == 200

    def test_a_repeated_entry_is_stored_once(
        self, admin_headers: dict[str, str], client: TestClient
    ):
        resp = client.post(
            FROM_CATALOGUE,
            headers=admin_headers,
            json={
                "entries": [
                    {"entry": "okta", "intake": "beats"},
                    {"entry": "okta", "intake": "beats"},
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [row["source"] for row in body["created"]] == ["okta"]
        assert body["failed"][0]["code"] == "conflict"
        assert "already exists" in body["failed"][0]["message"]

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
        resp = _create(client, admin_headers, case["entry"], case["body"])
        assert resp.status_code == case["expected_status"], resp.text

    def test_missing_variant(
        self, admin_headers: dict[str, str], client: TestClient, elastic_contract: None
    ):
        resp = _create(client, admin_headers, "o365-metrics-entra-alerts", {})
        saved = client.get("/api/v1/sources/o365-metrics-entra-alerts", headers=admin_headers)
        assert resp.status_code == 409, resp.text
        assert resp.json()["created"] == []
        assert resp.json()["failed"] == [
            {
                "entry": "o365-metrics-entra-alerts",
                "source": "o365-metrics-entra-alerts",
                "code": "missing_variant",
                "message": "the deployed 'dfe-transform-elastic' does not ship transform variant "
                "'filebeat.o365_metrics_entra_alerts.default'",
            }
        ]
        assert saved.status_code == 404

    @pytest.mark.parametrize(
        "case",
        CREATE_FROM_CATALOGUE_REJECT_CASES,
        ids=[case["id"] for case in CREATE_FROM_CATALOGUE_REJECT_CASES],
    )
    def test_rejects(
        self, admin_headers: dict[str, str], client: TestClient, case: CreateFromCatalogueRejectCase
    ):
        resp = _create(client, admin_headers, case["entry"], case["body"])
        expected = case["expected_response"]
        if case["id"] == "the_retired_transform_field":
            assert (resp.status_code, resp.json()) == (422, expected)
            return
        assert resp.status_code == 200, resp.text
        assert resp.json()["created"] == []
        assert resp.json()["failed"] == [
            {
                "entry": case["entry"],
                "source": "",
                "code": expected["code"],
                "message": expected["message"],
            }
        ]

    def test_twice(self, admin_headers: dict[str, str], client: TestClient):
        first = _create(client, admin_headers, "cisco-asa", {})
        again = _create(client, admin_headers, "cisco-asa", {})
        assert (first.status_code, again.status_code) == (201, 409)
        assert again.json()["created"] == []
        assert again.json()["failed"][0]["code"] == "conflict"

    def test_needs_source_write(self, client: TestClient):
        assert (
            client.post(FROM_CATALOGUE, json={"entries": [{"entry": "cisco-asa"}]}).status_code
            == 401
        )

    def test_a_viewer_may_not_create(self, client: TestClient, viewer_headers: dict[str, str]):
        resp = _create(client, viewer_headers, "cisco-asa", {})
        saved = client.get("/api/v1/sources/cisco-asa", headers=viewer_headers)
        assert (resp.status_code, saved.status_code) == (403, 404)
