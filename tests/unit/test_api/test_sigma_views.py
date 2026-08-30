#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_sigma_views.py
#  Purpose:      API tests for the Sigma source-view definition CRUD + generation
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""End-to-end Sigma view-definition API via TestClient - real gitcrud, no live CH.

Exercises the CRUD over the stored per-source view definition and the generate
action that renders DDL from it, including the JSON-derived column extraction.
"""

from __future__ import annotations

from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo


def _wire_gitcrud(app, tmp_path):
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    gc = GitCrud(repo, default_registry())
    app.state.gitcrud = gc
    return gc


def _view_body(**overrides) -> dict:
    body = {
        "description": "Windows process creation",
        "columns": [
            {"sigma_field": "EventID", "json_path": "EventID", "type": "UInt32"},
            {"sigma_field": "CommandLine", "json_path": "process.command_line"},
            {"sigma_field": "Image", "source_column": "process_name"},
        ],
        "include_source_columns": True,
    }
    body.update(overrides)
    return body


# -- Gate / auth ---------------------------------------------


def test_views_requires_auth(client):
    assert client.get("/api/v1/sigma/views").status_code == 401


def test_views_503_when_gitops_disabled(client, admin_headers):
    resp = client.get("/api/v1/sigma/views", headers=admin_headers)
    assert resp.status_code == 503
    assert resp.json()["code"] == "not_configured"


def test_viewer_cannot_write_view(client, app, viewer_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)
    put = client.put("/api/v1/sigma/views/windows-audit", json=_view_body(), headers=viewer_headers)
    assert put.status_code == 403
    delete = client.delete("/api/v1/sigma/views/windows-audit", headers=viewer_headers)
    assert delete.status_code == 403


# -- CRUD round trip -----------------------------------------


def test_crud_roundtrip(client, app, admin_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)

    created = client.put(
        "/api/v1/sigma/views/windows-audit", json=_view_body(), headers=admin_headers
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["source_name"] == "windows-audit"
    assert len(body["columns"]) == 3

    got = client.get("/api/v1/sigma/views/windows-audit", headers=admin_headers)
    assert got.status_code == 200
    assert got.json()["columns"][0]["json_path"] == "EventID"

    listing = client.get("/api/v1/sigma/views", headers=admin_headers)
    assert listing.status_code == 200
    rows = {r["source_name"]: r for r in listing.json()["items"]}
    assert rows["windows-audit"]["column_count"] == 3
    assert rows["windows-audit"]["json_derived_count"] == 2

    deleted = client.delete("/api/v1/sigma/views/windows-audit", headers=admin_headers)
    assert deleted.status_code == 204
    gone = client.get("/api/v1/sigma/views/windows-audit", headers=admin_headers)
    assert gone.status_code == 404


def test_get_missing_view_404(client, app, admin_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)
    assert client.get("/api/v1/sigma/views/nope", headers=admin_headers).status_code == 404


def test_delete_missing_view_404(client, app, admin_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)
    assert client.delete("/api/v1/sigma/views/nope", headers=admin_headers).status_code == 404


def test_put_rejects_invalid_column_422(client, app, admin_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)
    # a column with BOTH source_column and json_path is rejected by the model validator
    bad = _view_body(columns=[{"sigma_field": "X", "source_column": "c", "json_path": "p"}])
    resp = client.put("/api/v1/sigma/views/windows-audit", json=bad, headers=admin_headers)
    assert resp.status_code == 422


# -- Generate DDL from the stored definition (Task B end-to-end) --


def test_generate_ddl_from_stored_definition(client, app, admin_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)
    client.put("/api/v1/sigma/views/windows-audit", json=_view_body(), headers=admin_headers)

    gen = client.post("/api/v1/sigma/views/windows-audit", headers=admin_headers)
    assert gen.status_code == 200, gen.text
    ddl = gen.json()["ddl"]
    assert "CREATE OR REPLACE VIEW `default`.`windows-audit_sigma` AS" in ddl
    # JSON-derived columns extracted from _json with the dynamic-subcolumn idiom
    assert "CAST(assumeNotNull(_json).`EventID` AS UInt32) AS `EventID`" in ddl
    assert "assumeNotNull(_json).`process.command_line` AS `CommandLine`" in ddl
    # a real source column is a plain alias
    assert "`process_name` AS `Image`" in ddl


def test_generate_invalid_stored_type_returns_422(client, app, admin_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)
    # type is a free string at write time; an illegal CH type only fails at render
    bad = _view_body(columns=[{"sigma_field": "X", "json_path": "p", "type": "String; DROP"}])
    put = client.put("/api/v1/sigma/views/windows-audit", json=bad, headers=admin_headers)
    assert put.status_code == 200, put.text

    gen = client.post("/api/v1/sigma/views/windows-audit", headers=admin_headers)
    assert gen.status_code == 422
    assert gen.json()["code"] == "invalid_view"


def test_update_replaces_definition(client, app, admin_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)
    client.put("/api/v1/sigma/views/windows-audit", json=_view_body(), headers=admin_headers)

    replacement = _view_body(
        description="slimmed",
        columns=[{"sigma_field": "EventID", "json_path": "EventID"}],
    )
    upd = client.put("/api/v1/sigma/views/windows-audit", json=replacement, headers=admin_headers)
    assert upd.status_code == 200
    assert upd.json()["description"] == "slimmed"
    assert len(upd.json()["columns"]) == 1
