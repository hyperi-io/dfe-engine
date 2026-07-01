#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_authoring.py
#  Purpose:      Authoring router - HyperDX strip, scaffold, AI stub endpoints
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The authoring endpoints (compute-only; AI endpoints return stub results)."""

from __future__ import annotations


def test_from_hyperdx_strips_time_bounds(client, admin_headers):
    r = client.post(
        "/api/v1/authoring/from-hyperdx",
        json={"query": "SELECT * FROM t WHERE _timestamp >= 1 AND _timestamp < 9 AND x = 1"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["replaced"] is True
    assert "{window}" in r.json()["query"]


def test_scaffold_builds_select(client, admin_headers):
    r = client.post(
        "/api/v1/authoring/scaffold",
        json={"columns": ["a", "b"], "table": "`dfe`.`t`", "source": "okta"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert "SELECT a, b" in r.json()["sql"]
    assert "{window}" in r.json()["sql"]


def test_ai_review_returns_stub(client, admin_headers):
    r = client.post("/api/v1/authoring/ai/review", json={"sql": "SELECT 1"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["output"]["stub"] is True
    assert r.json()["output"]["proposed_query"] == "SELECT 1"


def test_ai_create_returns_stub(client, admin_headers):
    r = client.post(
        "/api/v1/authoring/ai/create", json={"prompt": "failed logins by ip"}, headers=admin_headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["output"]["stub"] is True
    assert "failed logins by ip" in r.json()["output"]["rationale"]


def test_ai_generate_vrl_returns_stub(client, admin_headers):
    r = client.post(
        "/api/v1/authoring/ai/generate-vrl",
        json={"samples": ["line one", "line two"]},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["output"]["stub"] is True
    assert r.json()["output"]["sample_count"] == 2


def test_requires_auth(client):
    assert client.post("/api/v1/authoring/from-hyperdx", json={"query": "x"}).status_code == 401
