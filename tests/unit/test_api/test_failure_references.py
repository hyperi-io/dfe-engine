#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_failure_references.py
#  Purpose:      A raw query or token failure answers with a reference, never the backend's text
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The raw query route and the bearer-token check answer with a reference to their log line.

The raw route answered ClickHouse's own error text, and a refused token was told
PyJWT's reason. Either can carry statement fragments, user names or what the
token held, so it goes to the log under a reference the answer names.
"""

import re
from collections.abc import Iterator

import pytest
from scalo.logger import logger

from dfe_engine.query import datasources

RAW = "/api/v1/queries/raw"
# What a backend might say, none of which belongs in an answer.
BACKEND_TEXT = "Code: 516. DB::Exception: default: Authentication failed: password_hash a1b2c3"
REFERENCE = re.compile(r"\(reference ([0-9a-f]{12})\)$")


@pytest.fixture
def warnings() -> Iterator[list]:
    captured: list = []
    handler_id = logger.add(captured.append, level="WARNING")
    yield captured
    logger.remove(handler_id)


def _logged(warnings: list, reference: str) -> list[dict]:
    """The lines carrying ``reference`` and the failure's text, so not the audit event naming it."""
    return [
        m.record["extra"]
        for m in warnings
        if m.record["extra"].get("reference") == reference and "error" in m.record["extra"]
    ]


class _FailingAdapter:
    def execute(self, query, params, timeout, max_rows=None):
        raise RuntimeError(BACKEND_TEXT)


def test_a_failed_raw_query_answers_a_reference_its_log_line_carries(
    client, admin_headers, monkeypatch, warnings
):
    monkeypatch.setattr(datasources, "get_adapter", lambda _name: _FailingAdapter())

    resp = client.post(
        RAW, json={"datasource": "clickhouse:default", "query": "SELECT 1"}, headers=admin_headers
    )

    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["code"] == "query_error"
    assert BACKEND_TEXT not in resp.text
    assert "a1b2c3" not in resp.text
    reference = REFERENCE.search(body["message"])
    assert reference is not None, body["message"]
    (logged,) = _logged(warnings, reference.group(1))
    assert logged["error"] == BACKEND_TEXT


@pytest.mark.parametrize(
    "token",
    ["not-a-jwt", "a.b.c", "eyJhbGciOiJub25lIn0.eyJzdWIiOiJhZG1pbiJ9."],
    ids=["no-segments", "garbage-segments", "alg-none"],
)
def test_a_refused_token_answers_a_reference_not_the_libraries_reason(client, warnings, token):
    resp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 401, resp.text
    assert resp.headers["WWW-Authenticate"] == "Bearer"
    body = resp.json()
    assert body["code"] == "unauthorized"
    assert body["message"].startswith("Invalid token (reference ")
    reference = REFERENCE.search(body["message"])
    assert reference is not None
    (logged,) = _logged(warnings, reference.group(1))
    assert logged["error"]
    assert logged["error"] not in body["message"]
