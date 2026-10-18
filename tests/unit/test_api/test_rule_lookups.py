#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_rule_lookups.py
#  Purpose:      A rule may look values up in a listed lookup database and nowhere else
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""POST, PUT and validate on /api/v1/rules accept a lookup only in ``hunts.lookup_databases``.

The hunt runner's ClickHouse user reads every org, so a subquery or a table after IN
answers from rows past the hunt's orgs. A database the deployment lists holds no
org's rows, such as a threat-intel ``threat`` database of indicators.
"""

import pytest

from dfe_engine.settings import DFESettings

LOOKUP = "SELECT * FROM dfe.main WHERE process_name IN (SELECT ioc FROM threat.iocs)"


@pytest.fixture
def api_settings(api_settings: DFESettings) -> DFESettings:
    """The shared test settings with ``threat`` listed as a lookup database."""
    hunts = api_settings.hunts.model_copy(update={"lookup_databases": ["threat"]})
    return api_settings.model_copy(update={"hunts": hunts})


def _payload(name: str, user_sql: str) -> dict:
    return {
        "name": name,
        "display_name": "IOC match",
        "severity": "high",
        "user_sql": user_sql,
        "source": "windows-audit",
    }


@pytest.mark.parametrize(
    "condition",
    [
        "process_name IN (SELECT ioc FROM threat.iocs)",
        "process_name NOT IN (SELECT ioc FROM threat.iocs WHERE kind = 'process')",
        "process_name IN threat.iocs",
        "process_name IN (threat.iocs)",
    ],
    ids=["subquery", "filtered_subquery", "table", "bracketed_table"],
)
def test_a_lookup_in_a_listed_database_is_saved(client, admin_headers, condition):
    resp = client.post(
        "/api/v1/rules",
        json=_payload("ioc_match", f"SELECT * FROM dfe.main WHERE {condition}"),
        headers=admin_headers,
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["sql_errors"] == []
    stored = client.get("/api/v1/rules/ioc_match", headers=admin_headers).json()
    assert stored["where_clause"] == condition
    assert stored["sql_errors"] == []


@pytest.mark.parametrize(
    "condition",
    [
        "process_name IN (SELECT ioc FROM intel.iocs)",
        "process_name IN (SELECT process_name FROM dfe.main)",
        "process_name IN intel.iocs",
        "process_name IN (intel.iocs)",
        "process_name IN (SELECT ioc FROM threat.iocs UNION ALL SELECT _source FROM dfe.main)",
    ],
    ids=["subquery", "data_database", "table", "bracketed_table", "union"],
)
def test_a_lookup_past_the_listed_databases_is_refused_and_never_written(
    client, admin_headers, condition
):
    resp = client.post(
        "/api/v1/rules",
        json=_payload("ioc_match", f"SELECT * FROM dfe.main WHERE {condition}"),
        headers=admin_headers,
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "invalid_sql"
    assert client.get("/api/v1/rules/ioc_match", headers=admin_headers).status_code == 404


def test_an_update_to_an_unlisted_database_keeps_the_stored_lookup(client, admin_headers):
    assert (
        client.post(
            "/api/v1/rules", json=_payload("ioc_match", LOOKUP), headers=admin_headers
        ).status_code
        == 201
    )

    resp = client.put(
        "/api/v1/rules/ioc_match",
        json=_payload("ioc_match", LOOKUP.replace("threat.iocs", "intel.iocs")),
        headers=admin_headers,
    )

    assert resp.status_code == 422, resp.text
    stored = client.get("/api/v1/rules/ioc_match", headers=admin_headers).json()
    assert stored["where_clause"] == "process_name IN (SELECT ioc FROM threat.iocs)"


def test_validate_takes_the_listed_databases_too(client, admin_headers):
    listed = client.post("/api/v1/rules/validate", json={"sql": LOOKUP}, headers=admin_headers)
    unlisted = client.post(
        "/api/v1/rules/validate",
        json={"sql": LOOKUP.replace("threat.iocs", "intel.iocs")},
        headers=admin_headers,
    )

    assert listed.json() == {"valid": True, "errors": []}
    assert unlisted.json()["valid"] is False
    assert any("intel" in error["message"] for error in unlisted.json()["errors"])
