#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_rules_source_name.py
#  Purpose:      POST and PUT /api/v1/rules refuse a source the runner cannot scan or no source carries
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A rule's FROM is stored as ``source_db`` and ``source_table`` and spliced into ``FROM``.

A backtick-quoted name reaches the store unquoted, so ``dfe.`main) -- x``` would
be stored as a table named ``main) -- x``, which the hunt runner drops at compile.
The API refuses it with 422 instead, naming the problem, and accepts a source
named for a hyphenated label. A ``source`` label must name a registered source,
whether the rule is written as SQL or as a ``where_clause`` on that source's table.
"""

import pytest

pytestmark = pytest.mark.usefixtures("rule_sources")

REFUSED_SQL = {
    "closing bracket and comment": "SELECT * FROM dfe.`main) -- x` WHERE severity = 'high'",
    "space": "SELECT * FROM dfe.`ma in` WHERE severity = 'high'",
    "quote in the database": "SELECT * FROM `d'fe`.main WHERE severity = 'high'",
    "dot in the table": "SELECT * FROM dfe.`a.b` WHERE severity = 'high'",
}

REFUSED_SOURCES = {
    "closing bracket and comment": ("main) -- x", "is not a table label"),
    "space": ("ma in", "is not a table label"),
    "quote": ("d'fe", "is not a table label"),
    "database qualified": ("dfe.main", "a label, not a <db>.<table>"),
    "dot in the label": ("a.b", "a label, not a <db>.<table>"),
    "no such source": ("never-registered", "not a registered source"),
}

_KEPT_SQL = "SELECT * FROM dfe.main WHERE severity = 'high'"


def _payload(name: str, user_sql: str) -> dict:
    return {"name": name, "display_name": "Source", "severity": "high", "user_sql": user_sql}


def _where_payload(name: str, source: str) -> dict:
    return {
        "name": name,
        "display_name": "Source",
        "severity": "high",
        "where_clause": "severity = 'high'",
        "source": source,
    }


def _assert_refused(resp) -> None:
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "invalid_sql"
    messages = [error["message"] for error in body["sql_errors"]]
    assert any("names no table the hunt runner can scan" in m for m in messages), messages
    assert any("is not a table name" in m for m in messages), messages


def _assert_refused_with(resp, fragment: str) -> None:
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "invalid_sql"
    messages = [error["message"] for error in body["sql_errors"]]
    assert any(fragment in m for m in messages), messages


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


@pytest.mark.parametrize(
    ("source", "fragment"), REFUSED_SOURCES.values(), ids=list(REFUSED_SOURCES)
)
def test_create_refuses_a_where_on_a_source_that_is_not_a_registered_label(
    client, admin_headers, source, fragment
):
    resp = client.post(
        "/api/v1/rules", json=_where_payload("bad_source", source), headers=admin_headers
    )

    _assert_refused_with(resp, fragment)
    assert client.get("/api/v1/rules/bad_source", headers=admin_headers).status_code == 404


@pytest.mark.parametrize(
    ("source", "fragment"), REFUSED_SOURCES.values(), ids=list(REFUSED_SOURCES)
)
def test_update_refuses_that_source_and_keeps_the_stored_one(
    client, admin_headers, source, fragment
):
    created = client.post(
        "/api/v1/rules", json=_where_payload("kept_source", "events"), headers=admin_headers
    )
    assert created.status_code == 201, created.text

    resp = client.put(
        "/api/v1/rules/kept_source",
        json=_where_payload("kept_source", source),
        headers=admin_headers,
    )

    _assert_refused_with(resp, fragment)
    stored = client.get("/api/v1/rules/kept_source", headers=admin_headers).json()
    assert (stored["source"], stored["source_db"], stored["source_table"]) == (
        "events",
        "dfe",
        "events",
    )


def test_sql_naming_an_unregistered_source_label_is_refused(client, admin_headers):
    resp = client.post(
        "/api/v1/rules",
        json={**_payload("unregistered", _KEPT_SQL), "source": "never-registered"},
        headers=admin_headers,
    )

    _assert_refused_with(resp, "not a registered source")
    assert client.get("/api/v1/rules/unregistered", headers=admin_headers).status_code == 404


def test_a_where_on_a_hyphenated_label_reads_that_table(client, admin_headers):
    resp = client.post(
        "/api/v1/rules", json=_where_payload("windows", "windows-audit"), headers=admin_headers
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["sql_errors"] == []
    stored = client.get("/api/v1/rules/windows", headers=admin_headers).json()
    assert (stored["source_db"], stored["source_table"]) == ("dfe", "windows-audit")
