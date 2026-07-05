#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_lifecycle.py
#  Purpose:      Lifecycle API - tiers, per-service RBAC, gitops dial
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The lifecycle router over a real local gitops repo (no mocks)."""

from __future__ import annotations

from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance import PolicyStore


def _wire_gitcrud(app, tmp_path):
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    gc = GitCrud(repo, default_registry())
    app.state.gitcrud = gc
    app.state.policy_store = PolicyStore(gc)
    return gc


def _scoped_headers(app, api_settings, *, username, role, permissions):
    from dfe_engine.api.deps import create_access_token

    role_store = app.state.role_store
    group_store = app.state.group_store
    account_store = app.state.account_store
    if role_store.get(role) is None:
        role_store.create(role, description="test", permissions=permissions)
    app.state.role_config = role_store.load_config()
    gname = f"grp-{username}"
    try:
        group_store.create(gname, roles=[role])
    except ValueError:
        group_store.update(gname, roles=[role])
    if account_store.get(username) is None:
        account_store.create(username, "pw-12345", groups=[gname])
    group_store.add_member(gname, username)
    token = create_access_token(
        data={"sub": username, "org_id": "test-org", "roles": [role], "groups": [gname]},
        settings=api_settings,
    )
    return {"Authorization": f"Bearer {token}"}


def test_admin_can_stop_app_and_pause_backing(client, app, admin_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)
    r = client.post("/api/v1/lifecycle/receiver", json={"state": "stopped"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "stopped"
    assert r.json()["commit_sha"]
    assert r.json()["pending_reconcile"] is True
    # a backing service, as admin (wildcard)
    rk = client.post("/api/v1/lifecycle/kafka", json={"state": "paused"}, headers=admin_headers)
    assert rk.status_code == 200, rk.text
    assert app.state.gitcrud.get("helmvars", "kafka-default-values")["state"] == "paused"


def test_lifecycle_writes_consumed_overlay_file(client, app, admin_headers, tmp_path):
    """The dial lands in values/receiver-default-values.yaml (the file the
    dfe-infra ApplicationSet actually globs), as a top-level `state:` key;
    the never-consumed values/receiver.yaml must NOT appear."""
    gc = _wire_gitcrud(app, tmp_path)
    r = client.post("/api/v1/lifecycle/receiver", json={"state": "stopped"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    consumed = gc.repo_path / "values" / "receiver-default-values.yaml"
    assert consumed.is_file()
    assert gc.get("helmvars", "receiver-default-values")["state"] == "stopped"
    assert not (gc.repo_path / "values" / "receiver.yaml").exists()


def test_lifecycle_pending_reconcile_reflects_reality(client, app, admin_headers, tmp_path):
    """A no-op write (same state again) is not pending anything."""
    _wire_gitcrud(app, tmp_path)
    first = client.post(
        "/api/v1/lifecycle/receiver", json={"state": "stopped"}, headers=admin_headers
    )
    assert first.json()["pending_reconcile"] is True
    again = client.post(
        "/api/v1/lifecycle/receiver", json={"state": "stopped"}, headers=admin_headers
    )
    assert again.status_code == 200, again.text
    assert again.json()["changed"] is False
    assert again.json()["pending_reconcile"] is False


def test_pinned_service_has_no_lifecycle_api(client, app, admin_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)
    r = client.post(
        "/api/v1/lifecycle/clickhouse", json={"state": "stopped"}, headers=admin_headers
    )
    assert r.status_code == 404


def test_unknown_service_404(client, app, admin_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)
    r = client.post("/api/v1/lifecycle/nope", json={"state": "stopped"}, headers=admin_headers)
    assert r.status_code == 404


def test_operator_can_stop_app_but_not_backing(client, app, api_settings, tmp_path):
    _wire_gitcrud(app, tmp_path)
    op = _scoped_headers(
        app,
        api_settings,
        username="op1",
        role="app-operator",
        permissions=["lifecycle:read", "lifecycle:app:receiver"],
    )
    ok = client.post("/api/v1/lifecycle/receiver", json={"state": "stopped"}, headers=op)
    assert ok.status_code == 200, ok.text
    # a backing service needs lifecycle:backing:* (admin/infra) -> 403
    denied = client.post("/api/v1/lifecycle/kafka", json={"state": "stopped"}, headers=op)
    assert denied.status_code == 403


def test_list_services_shows_tier_and_state(client, app, admin_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)
    r = client.get("/api/v1/lifecycle", headers=admin_headers)
    assert r.status_code == 200
    by = {s["name"]: s for s in r.json()}
    assert by["clickhouse"]["state"] == "pinned"
    assert by["receiver"]["tier"] == "managed_app"
    assert by["kafka"]["tier"] == "managed_backing"


def test_requires_auth(client):
    assert client.get("/api/v1/lifecycle").status_code == 401
