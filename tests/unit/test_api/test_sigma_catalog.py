#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_sigma_catalog.py
#  Purpose:      API tests for the sigma provider + catalogue + selection endpoints
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""End-to-end sigma catalogue API via TestClient - real gitcrud, real local provider.

The provider is a local-files provider pointed at a tmp import dir (no network); the
sync runs through the real task manager into the real id-keyed gitcrud store.
"""

from __future__ import annotations

from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo

_UUID = "dddddddd-dddd-dddd-dddd-dddddddddddd"


def _wire_gitcrud(app, tmp_path):
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    gc = GitCrud(repo, default_registry())
    app.state.gitcrud = gc
    return gc


def _rule_yaml(modified: str = "2023-05-01", level: str = "high") -> str:
    return f"""title: Api Rule
id: {_UUID}
status: experimental
logsource:
    category: process_creation
    product: windows
detection:
    selection:
        Image|endswith: \\evil.exe
    condition: selection
level: {level}
date: 2022-01-01
modified: {modified}
"""


def _import_dir(tmp_path, modified="2023-05-01", level="high"):
    d = tmp_path / "import"
    d.mkdir(exist_ok=True)
    (d / "a.yml").write_text(_rule_yaml(modified, level), encoding="utf-8")
    return d


def _register_local_provider(client, headers, directory):
    return client.post(
        "/api/v1/sigma/providers",
        json={"name": "files", "kind": "local_files", "options": {"directory": str(directory)}},
        headers=headers,
    )


# -- Gate / auth ---------------------------------------------


def test_catalogue_503_when_gitops_disabled(client, admin_headers):
    resp = client.get("/api/v1/sigma/catalogue", headers=admin_headers)
    assert resp.status_code == 503
    assert resp.json()["code"] == "not_configured"


def test_requires_auth(client):
    assert client.get("/api/v1/sigma/catalogue").status_code == 401
    assert client.get("/api/v1/sigma/providers").status_code == 401


def test_viewer_cannot_write(client, app, viewer_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)
    reg = _register_local_provider(client, viewer_headers, tmp_path / "import")
    assert reg.status_code == 403
    sel = client.post(f"/api/v1/sigma/catalogue/{_UUID}/select", headers=viewer_headers)
    assert sel.status_code == 403


# -- Provider CRUD -------------------------------------------


def test_list_providers_includes_builtin_default(client, app, admin_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)
    resp = client.get("/api/v1/sigma/providers", headers=admin_headers)
    assert resp.status_code == 200
    names = {p["name"] for p in resp.json()}
    assert "sigmahq" in names


def test_register_get_enable_disable_provider(client, app, admin_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)
    reg = _register_local_provider(client, admin_headers, _import_dir(tmp_path))
    assert reg.status_code == 201, reg.text

    got = client.get("/api/v1/sigma/providers/files", headers=admin_headers)
    assert got.status_code == 200
    assert got.json()["kind"] == "local_files"

    disabled = client.post("/api/v1/sigma/providers/files/disable", headers=admin_headers)
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    # a disabled provider refuses a manual sync
    blocked = client.post("/api/v1/sigma/providers/files/sync", headers=admin_headers)
    assert blocked.status_code == 409

    enabled = client.post("/api/v1/sigma/providers/files/enable", headers=admin_headers)
    assert enabled.json()["enabled"] is True


def test_provider_reach_rejects_ssrf_host(client, app, admin_headers, tmp_path):
    # M1: a valhalla base_url / git url at a loopback or link-local host (the cloud
    # metadata endpoint 169.254.169.254 is link-local) is rejected even for an admin
    # - defence in depth on the SSRF surface (the valhalla base_url was previously
    # unvalidated, bypassing the allow-list entirely).
    _wire_gitcrud(app, tmp_path)
    for kind, opts in (
        ("valhalla", {"base_url": "http://169.254.169.254"}),
        ("git_repo", {"url": "http://127.0.0.1/x.git"}),
    ):
        resp = client.post(
            "/api/v1/sigma/providers",
            json={"name": "evil", "kind": kind, "options": opts},
            headers=admin_headers,
        )
        assert resp.status_code == 422, (kind, resp.text)
        assert resp.json()["code"] == "host_blocked", (kind, resp.text)


def test_unknown_provider_404(client, app, admin_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)
    assert client.get("/api/v1/sigma/providers/nope", headers=admin_headers).status_code == 404


# -- Sync -> catalogue -> select -----------------------------


def test_sync_populates_catalogue_and_select_flow(client, app, admin_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)
    _register_local_provider(client, admin_headers, _import_dir(tmp_path))

    synced = client.post("/api/v1/sigma/providers/files/sync?wait=30", headers=admin_headers)
    assert synced.status_code == 200, synced.text
    body = synced.json()
    assert body["status"] == "completed"
    assert body["report"]["added"] == 1

    listing = client.get("/api/v1/sigma/catalogue", headers=admin_headers)
    assert listing.status_code == 200
    page = listing.json()
    assert page["total"] == 1
    assert page["items"][0]["id"] == _UUID
    assert page["items"][0]["origin"] == "file"
    assert page["items"][0]["selected"] is False

    detail = client.get(f"/api/v1/sigma/catalogue/{_UUID}", headers=admin_headers)
    assert detail.status_code == 200
    assert detail.json()["provenance"]["origin"] == "file"
    assert "detection" in detail.json()["rule"]

    # select -> appears in /selected and the catalogue row flips
    sel = client.post(f"/api/v1/sigma/catalogue/{_UUID}/select", headers=admin_headers)
    assert sel.status_code == 200
    assert sel.json()["rules"] == [_UUID]
    assert client.get("/api/v1/sigma/selected", headers=admin_headers).json()["rules"] == [_UUID]
    only_sel = client.get("/api/v1/sigma/catalogue?selected=true", headers=admin_headers)
    assert only_sel.json()["total"] == 1

    # deselect
    desel = client.post(f"/api/v1/sigma/catalogue/{_UUID}/deselect", headers=admin_headers)
    assert desel.json()["rules"] == []


def test_select_unknown_rule_404(client, app, admin_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)
    resp = client.post(f"/api/v1/sigma/catalogue/{_UUID}/select", headers=admin_headers)
    assert resp.status_code == 404


def test_edit_then_resync_preserves_local_edit(client, app, admin_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)
    import_dir = _import_dir(tmp_path, modified="2023-05-01", level="high")
    _register_local_provider(client, admin_headers, import_dir)
    client.post("/api/v1/sigma/providers/files/sync?wait=30", headers=admin_headers)

    # operator edits the rule (drops level, renames)
    rule = client.get(f"/api/v1/sigma/catalogue/{_UUID}", headers=admin_headers).json()["rule"]
    rule["level"] = "informational"
    edited = client.put(
        f"/api/v1/sigma/catalogue/{_UUID}",
        json={"rule": rule, "title": "Renamed"},
        headers=admin_headers,
    )
    assert edited.status_code == 200
    assert edited.json()["provenance"]["local_edited"] is True

    # upstream changes the file, re-sync
    (import_dir / "a.yml").write_text(_rule_yaml(modified="2024-01-01", level="critical"), "utf-8")
    resync = client.post("/api/v1/sigma/providers/files/sync?wait=30", headers=admin_headers)
    assert resync.json()["report"]["merged"] == 1

    final = client.get(f"/api/v1/sigma/catalogue/{_UUID}", headers=admin_headers).json()
    # local edit survived; drift recorded against the new upstream date
    assert final["rule"]["level"] == "informational"
    assert final["title"] == "Renamed"
    assert final["provenance"]["local_edited"] is True
    assert final["provenance"]["drift"] is True


def test_edit_unknown_rule_404(client, app, admin_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)
    resp = client.put(
        f"/api/v1/sigma/catalogue/{_UUID}",
        json={"rule": {"title": "x"}},
        headers=admin_headers,
    )
    assert resp.status_code == 404
