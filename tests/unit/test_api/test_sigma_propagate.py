#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_sigma_propagate.py
#  Purpose:      API tests for sigma propagation (selected rules -> rules + hunts)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""End-to-end sigma propagation API via TestClient - real gitcrud + registries.

A local-files provider (no network) imports a rule into the real gitcrud
catalogue; the rule is selected, then POST /sigma/propagate generates a DFE rule
over the source's {source}_sigma view + a per-source hunt. No live ClickHouse.
"""

from __future__ import annotations

from dfe_engine.api.deps import _registries
from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo

_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
_BINDING = f"sigma_windows_audit_{_ID.replace('-', '')}"


def _wire_gitcrud(app, tmp_path):
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    gc = GitCrud(repo, default_registry())
    app.state.gitcrud = gc
    return gc


def _add_windows_source(name: str = "windows_audit") -> None:
    _registries["source"].save_source(
        {
            "source": name,
            "enabled": True,
            "match": {"field": "tags.collector.type", "value": name},
            "schema": {"engine": "MergeTree"},
            "sigma": {"taxonomy": "windows", "custom_mappings": {}},
        }
    )


def _rule_yaml() -> str:
    return f"""title: Suspicious Certutil
id: {_ID}
status: experimental
logsource:
    category: process_creation
    product: windows
detection:
    selection:
        Image|endswith: \\certutil.exe
    condition: selection
level: high
date: 2022-01-01
modified: 2023-05-01
"""


def _seed_selected(client, headers, tmp_path) -> None:
    """Import a rule via a local-files provider and select it (real catalogue)."""
    import_dir = tmp_path / "import"
    import_dir.mkdir(exist_ok=True)
    (import_dir / "a.yml").write_text(_rule_yaml(), encoding="utf-8")
    client.post(
        "/api/v1/sigma/providers",
        json={"name": "files", "kind": "local_files", "options": {"directory": str(import_dir)}},
        headers=headers,
    )
    synced = client.post("/api/v1/sigma/providers/files/sync?wait=30", headers=headers)
    assert synced.status_code == 200, synced.text
    sel = client.post(f"/api/v1/sigma/catalogue/{_ID}/select", headers=headers)
    assert sel.status_code == 200, sel.text


# ── Gate / auth ─────────────────────────────────────────────


def test_propagate_requires_auth(client):
    assert client.post("/api/v1/sigma/propagate").status_code == 401
    assert client.get("/api/v1/sigma/bindings").status_code == 401


def test_propagate_503_when_gitops_disabled(client, admin_headers):
    resp = client.post("/api/v1/sigma/propagate", headers=admin_headers)
    assert resp.status_code == 503
    assert resp.json()["code"] == "not_configured"


def test_viewer_cannot_propagate(client, app, viewer_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)
    assert client.post("/api/v1/sigma/propagate", headers=viewer_headers).status_code == 403
    assert (
        client.delete(f"/api/v1/sigma/bindings/{_BINDING}", headers=viewer_headers).status_code
        == 403
    )


# ── Propagate -> bindings -> hunt (Task A + B + C) ──────────


def test_propagate_generates_binding_and_hunt(client, app, admin_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)
    _add_windows_source()
    _seed_selected(client, admin_headers, tmp_path)

    resp = client.post(
        "/api/v1/sigma/propagate", json={"hunt_customers": ["acme"]}, headers=admin_headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    report = body["report"]
    assert report["created"] == [_BINDING]
    assert report["hunts_touched"] == ["sigma_hunt_windows_audit"]

    # the generated DFE rule targets the sigma view + carries the back-ref
    rule = client.get(f"/api/v1/rules/{_BINDING}", headers=admin_headers).json()
    assert rule["source_table"] == "windows_audit_sigma"
    assert "Image ILIKE" in rule["where_clause"]

    # the per-source hunt includes the binding
    hunt = client.get("/api/v1/hunts/sigma_hunt_windows_audit", headers=admin_headers).json()
    assert [r["rule_name"] for r in hunt["rules"]] == [_BINDING]
    assert hunt["customers"] == ["acme"]

    # bindings list + get
    listing = client.get("/api/v1/sigma/bindings", headers=admin_headers).json()
    assert listing["total"] == 1
    assert listing["items"][0]["rule_id"] == _BINDING
    assert listing["items"][0]["drift"] is False

    got = client.get(f"/api/v1/sigma/bindings/{_BINDING}", headers=admin_headers)
    assert got.status_code == 200
    assert got.json()["sigma_rule_id"] == _ID
    assert got.json()["hunts"] == ["sigma_hunt_windows_audit"]


def test_propagate_poll_endpoint(client, app, admin_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)
    _add_windows_source()
    _seed_selected(client, admin_headers, tmp_path)

    submitted = client.post("/api/v1/sigma/propagate?wait=0", headers=admin_headers)
    assert submitted.status_code == 200
    task_id = submitted.json()["task_id"]

    polled = client.get(f"/api/v1/sigma/propagations/{task_id}", headers=admin_headers)
    assert polled.status_code == 200
    assert polled.json()["task_id"] == task_id
    assert client.get("/api/v1/sigma/propagations/nope", headers=admin_headers).status_code == 404


def test_propagate_reports_drift_and_force(client, app, admin_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)
    _add_windows_source()
    _seed_selected(client, admin_headers, tmp_path)
    client.post("/api/v1/sigma/propagate", headers=admin_headers)

    # adopt the sigma rule (local edit) -> a plain re-propagate skips the binding
    client.post(f"/api/v1/sigma/catalogue/{_ID}/adopt", headers=admin_headers)
    again = client.post("/api/v1/sigma/propagate", headers=admin_headers).json()
    assert [s["rule_id"] for s in again["report"]["skipped_drifted"]] == [_BINDING]
    assert client.get("/api/v1/sigma/bindings", headers=admin_headers).json()["items"][0]["drift"]

    forced = client.post(
        "/api/v1/sigma/propagate", json={"force": True}, headers=admin_headers
    ).json()
    assert forced["report"]["updated"] == [_BINDING]


def test_delete_binding_unlinks_hunt(client, app, admin_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)
    _add_windows_source()
    _seed_selected(client, admin_headers, tmp_path)
    client.post("/api/v1/sigma/propagate", headers=admin_headers)

    deleted = client.delete(f"/api/v1/sigma/bindings/{_BINDING}", headers=admin_headers)
    assert deleted.status_code == 204
    assert client.get(f"/api/v1/rules/{_BINDING}", headers=admin_headers).status_code == 404
    # the emptied per-source hunt is gone
    assert (
        client.get("/api/v1/hunts/sigma_hunt_windows_audit", headers=admin_headers).status_code
        == 404
    )


def test_delete_unknown_binding_404(client, app, admin_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)
    _add_windows_source()
    resp = client.delete(f"/api/v1/sigma/bindings/{_BINDING}", headers=admin_headers)
    assert resp.status_code == 404


def test_get_binding_404_for_missing(client, app, admin_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)
    assert (
        client.get(f"/api/v1/sigma/bindings/{_BINDING}", headers=admin_headers).status_code == 404
    )


def test_propagate_no_selection_is_empty_report(client, app, admin_headers, tmp_path):
    _wire_gitcrud(app, tmp_path)
    _add_windows_source()
    resp = client.post("/api/v1/sigma/propagate", headers=admin_headers)
    assert resp.status_code == 200
    report = resp.json()["report"]
    assert report["total_selected"] == 0
    assert report["created"] == []
