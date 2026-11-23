#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_rules_source_name.py
#  Purpose:      POST and PUT /api/v1/rules refuse a source that is no registered table label
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A rule's ``source`` is a label, and its table is the label in the data database.

The label is spliced into ``FROM`` quoted, and stored as ``source_db`` and ``source_table``.
A name that is no plain table label, or a label no source carries, is refused with 422
and nothing is written. A hyphenated label is a table name too, so it is accepted.
"""

import pytest

pytestmark = pytest.mark.usefixtures("rule_sources")

REFUSED_SOURCES = {
    "closing bracket and comment": ("main) -- x", "is not a table label"),
    "space": ("ma in", "is not a table label"),
    "quote": ("d'fe", "is not a table label"),
    "database qualified": ("dfe.main", "a label, not a <db>.<table>"),
    "dot in the label": ("a.b", "a label, not a <db>.<table>"),
    "no such source": ("never-registered", "not a registered source"),
}


def _payload(name: str, source: str) -> dict:
    return {
        "name": name,
        "display_name": "Source",
        "severity": "high",
        "where_clause": "severity = 'high'",
        "source": source,
    }


def _assert_refused(resp, fragment: str) -> None:
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "invalid_sql"
    messages = [error["message"] for error in body["sql_errors"]]
    assert any(fragment in m for m in messages), messages


@pytest.mark.parametrize(
    ("source", "fragment"), REFUSED_SOURCES.values(), ids=list(REFUSED_SOURCES)
)
def test_create_refuses_a_source_that_is_not_a_registered_label(
    client, admin_headers, source, fragment
):
    resp = client.post("/api/v1/rules", json=_payload("bad_source", source), headers=admin_headers)

    _assert_refused(resp, fragment)
    assert client.get("/api/v1/rules/bad_source", headers=admin_headers).status_code == 404


@pytest.mark.parametrize(
    ("source", "fragment"), REFUSED_SOURCES.values(), ids=list(REFUSED_SOURCES)
)
def test_update_refuses_it_and_keeps_the_stored_source(client, admin_headers, source, fragment):
    created = client.post(
        "/api/v1/rules", json=_payload("kept_source", "events"), headers=admin_headers
    )
    assert created.status_code == 201, created.text

    resp = client.put(
        "/api/v1/rules/kept_source", json=_payload("kept_source", source), headers=admin_headers
    )

    _assert_refused(resp, fragment)
    stored = client.get("/api/v1/rules/kept_source", headers=admin_headers).json()
    assert (stored["source"], stored["source_db"], stored["source_table"]) == (
        "events",
        "dfe",
        "events",
    )


@pytest.mark.parametrize(
    ("source", "fragment"), REFUSED_SOURCES.values(), ids=list(REFUSED_SOURCES)
)
def test_check_reports_the_source_without_creating_anything(
    client, admin_headers, source, fragment
):
    resp = client.post(
        "/api/v1/rules/check",
        json={"source": source, "where_clause": "severity = 'high'"},
        headers=admin_headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is False
    assert any(fragment in e["message"] for e in body["errors"])
    assert client.get("/api/v1/rules", headers=admin_headers).json()["total_size"] == 0


def test_a_source_named_for_a_hyphenated_label_is_accepted(client, admin_headers):
    resp = client.post(
        "/api/v1/rules", json=_payload("windows", "windows-audit"), headers=admin_headers
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["sql_errors"] == []
    stored = client.get("/api/v1/rules/windows", headers=admin_headers).json()
    assert (stored["source_db"], stored["source_table"]) == ("dfe", "windows-audit")
