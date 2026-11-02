#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_rules_cel_refusal.py
#  Purpose:      A rule whose CEL filter does not transpile is refused with 422, never a 500
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""POST and PUT /api/v1/rules refuse a CEL filter the transpiler rejects.

The filter is transpiled to the rule's ClickHouse WHERE when the rule is written,
so a filter that does not parse, or that the hunt profile refuses, is a refusal of
the author's input, reported the way refused SQL is.
"""

import pytest

REFUSED_CEL = {
    "invalid": ("severity ==", "Failed to parse expression"),
    "profile violation": ("timestamp(event_time) > 0", "not allowed in the expression profile"),
}

_SQL = "SELECT * FROM dfe.main WHERE severity = 'high'"


def _payload(name: str, cel_filter: str) -> dict:
    return {
        "name": name,
        "display_name": "CEL",
        "severity": "high",
        "user_sql": _SQL,
        "cel_filter": cel_filter,
        "source": "windows-audit",
    }


def _assert_refused(resp, reason: str) -> None:
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "invalid_sql"
    (error,) = body["context"]["sql_errors"]
    assert error["message"].startswith("CEL filter refused: ")
    assert reason in error["message"]
    assert "double()" in error["suggestion"]


@pytest.mark.parametrize(("cel_filter", "reason"), REFUSED_CEL.values(), ids=list(REFUSED_CEL))
def test_create_refuses_the_filter_and_writes_nothing(client, admin_headers, cel_filter, reason):
    resp = client.post("/api/v1/rules", json=_payload("cel", cel_filter), headers=admin_headers)

    _assert_refused(resp, reason)
    assert client.get("/api/v1/rules/cel", headers=admin_headers).status_code == 404


@pytest.mark.parametrize(("cel_filter", "reason"), REFUSED_CEL.values(), ids=list(REFUSED_CEL))
def test_update_refuses_it_and_keeps_the_stored_rule(client, admin_headers, cel_filter, reason):
    created = client.post(
        "/api/v1/rules", json=_payload("kept_cel", "severity == 'high'"), headers=admin_headers
    )
    assert created.status_code == 201, created.text

    resp = client.put(
        "/api/v1/rules/kept_cel", json=_payload("kept_cel", cel_filter), headers=admin_headers
    )

    _assert_refused(resp, reason)
    stored = client.get("/api/v1/rules/kept_cel", headers=admin_headers).json()
    assert stored["cel_filter"] == "severity == 'high'"
