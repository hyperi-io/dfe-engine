#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_rules_source_name.py
#  Purpose:      POST and PUT /api/v1/rules refuse a FROM that names no table the runner can scan
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A rule's FROM is stored as ``source_db`` and ``source_table`` and spliced into ``FROM``.

A backtick-quoted name reaches the store unquoted, so ``dfe.`main) -- x``` would
be stored as a table named ``main) -- x``, which the hunt runner drops at compile.
The API refuses it with 422 instead, naming the problem, and accepts a source
named for a hyphenated label.
"""

import pytest

REFUSED_SQL = {
    "closing bracket and comment": "SELECT * FROM dfe.`main) -- x` WHERE severity = 'high'",
    "space": "SELECT * FROM dfe.`ma in` WHERE severity = 'high'",
    "quote in the database": "SELECT * FROM `d'fe`.main WHERE severity = 'high'",
    "dot in the table": "SELECT * FROM dfe.`a.b` WHERE severity = 'high'",
}

_KEPT_SQL = "SELECT * FROM dfe.main WHERE severity = 'high'"


def _payload(name: str, user_sql: str) -> dict:
    return {"name": name, "display_name": "Source", "severity": "high", "user_sql": user_sql}


def _assert_refused(resp) -> None:
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "invalid_sql"
    messages = [error["message"] for error in body["context"]["sql_errors"]]
    assert any("names no table the hunt runner can scan" in m for m in messages), messages
    assert any("is not a table name" in m for m in messages), messages


@pytest.mark.parametrize("user_sql", REFUSED_SQL.values(), ids=list(REFUSED_SQL))
def test_create_refuses_a_source_that_is_not_a_table_name(client, admin_headers, user_sql):
    resp = client.post(
        "/api/v1/rules", json=_payload("bad_source", user_sql), headers=admin_headers
    )

    _assert_refused(resp)
    assert client.get("/api/v1/rules/bad_source", headers=admin_headers).status_code == 404


@pytest.mark.parametrize("user_sql", REFUSED_SQL.values(), ids=list(REFUSED_SQL))
def test_update_refuses_it_and_keeps_the_stored_source(client, admin_headers, user_sql):
    created = client.post(
        "/api/v1/rules", json=_payload("kept_source", _KEPT_SQL), headers=admin_headers
    )
    assert created.status_code == 201, created.text

    resp = client.put(
        "/api/v1/rules/kept_source", json=_payload("kept_source", user_sql), headers=admin_headers
    )

    _assert_refused(resp)
    stored = client.get("/api/v1/rules/kept_source", headers=admin_headers).json()
    assert (stored["source_db"], stored["source_table"]) == ("dfe", "main")


def test_validate_reports_the_source_without_creating_anything(client, admin_headers):
    resp = client.post(
        "/api/v1/rules/validate",
        json={"sql": REFUSED_SQL["closing bracket and comment"]},
        headers=admin_headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is False
    assert any("'dfe.main) -- x' is not a table name" in e["message"] for e in body["errors"])


def test_a_source_named_for_a_hyphenated_label_is_accepted(client, admin_headers):
    resp = client.post(
        "/api/v1/rules",
        json=_payload("cisco", "SELECT * FROM dfe.`cisco-ios` WHERE severity = 'high'"),
        headers=admin_headers,
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["sql_errors"] == []
    stored = client.get("/api/v1/rules/cisco", headers=admin_headers).json()
    assert (stored["source_db"], stored["source_table"]) == ("dfe", "cisco-ios")
