#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_ch_rbac_reconcile_trigger.py
#  Purpose:      An org or group change provisions ClickHouse without a restart
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Every org and group write path asks for a CH RBAC reconcile, and none waits for it.

Before this, only startup and the manual governance endpoint ran the reconcile, so
a new org's users got 503 ``org_unprovisioned`` from the HyperDX connection
endpoint until one of them did. The app's trigger is swapped for one whose
reconcile is a call recorder, so each test counts the runs a write caused.
"""

import threading

import pytest

from dfe_engine.governance.ch import ReconcileResult, ReconcileTrigger

_WAIT = 10.0
_SCIM = "/api/v1/scim/v2"
_SCIM_GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"


class _Recorder:
    """A reconcile that counts its calls, and fails them while ``down`` is set."""

    def __init__(self) -> None:
        self.calls = 0
        self.down = False
        self._lock = threading.Lock()

    def __call__(self) -> ReconcileResult:
        with self._lock:
            self.calls += 1
        if self.down:
            raise ConnectionRefusedError("clickhouse is down")
        return ReconcileResult()


@pytest.fixture
def reconciles(app, client):
    """The app's trigger, reconciling into a recorder; settles long enough for a burst."""
    assert app.state.ch_rbac_reconcile is not None
    recorder = _Recorder()
    app.state.ch_rbac_reconcile = ReconcileTrigger(recorder, settle_seconds=1.0)
    return recorder


def _settled(app) -> None:
    assert app.state.ch_rbac_reconcile.wait_idle(_WAIT)


class TestOrgs:
    def test_an_org_and_its_group_created_together_are_one_reconcile(
        self, app, client, admin_headers, reconciles
    ):
        """The defect: the org's pinned CH user waited for a restart or a manual run."""
        org = client.post("/api/v1/orgs", json={"name": "acme"}, headers=admin_headers)
        group = client.post(
            "/api/v1/auth/groups",
            json={"name": "acme-analysts", "roles": [], "scope": "org:acme"},
            headers=admin_headers,
        )

        assert org.status_code == 201, org.text
        assert group.status_code == 201, group.text
        _settled(app)
        assert reconciles.calls == 1

    def test_update_and_delete_each_reconcile(self, app, client, admin_headers, reconciles):
        client.post("/api/v1/orgs", json={"name": "acme"}, headers=admin_headers)
        _settled(app)

        resp = client.put(
            "/api/v1/orgs/acme", json={"org_ids": ["acme", "a2"]}, headers=admin_headers
        )
        assert resp.status_code == 200, resp.text
        _settled(app)
        assert reconciles.calls == 2

        resp = client.delete("/api/v1/orgs/acme", headers=admin_headers)
        assert resp.status_code == 204, resp.text
        _settled(app)
        assert reconciles.calls == 3

    def test_a_failed_reconcile_does_not_fail_the_create(
        self, app, client, admin_headers, reconciles
    ):
        """ClickHouse down: the org is created, and the next change runs the reconcile again."""
        reconciles.down = True

        resp = client.post("/api/v1/orgs", json={"name": "acme"}, headers=admin_headers)

        assert resp.status_code == 201, resp.text
        assert app.state.org_registry.get("acme") is not None
        _settled(app)
        assert reconciles.calls == 1

        reconciles.down = False
        client.put("/api/v1/orgs/acme", json={"display_name": "Acme"}, headers=admin_headers)
        _settled(app)
        assert reconciles.calls == 2

    def test_a_refused_create_does_not_reconcile(self, app, client, admin_headers, reconciles):
        client.post("/api/v1/orgs", json={"name": "acme"}, headers=admin_headers)
        _settled(app)

        resp = client.post("/api/v1/orgs", json={"name": "acme"}, headers=admin_headers)

        assert resp.status_code == 409
        _settled(app)
        assert reconciles.calls == 1


class TestGroups:
    def test_create_update_and_delete_each_reconcile(self, app, client, admin_headers, reconciles):
        base = "/api/v1/auth/groups"
        resp = client.post(base, json={"name": "soc", "roles": []}, headers=admin_headers)
        assert resp.status_code == 201, resp.text
        _settled(app)
        assert reconciles.calls == 1

        resp = client.put(f"{base}/soc", json={"roles": ["data_viewer"]}, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        _settled(app)
        assert reconciles.calls == 2

        resp = client.delete(f"{base}/soc", headers=admin_headers)
        assert resp.status_code == 204, resp.text
        _settled(app)
        assert reconciles.calls == 3

    def test_membership_alone_does_not_reconcile(self, app, client, admin_headers, reconciles):
        """Members are not an input to the CH users, so a member change runs nothing."""
        base = "/api/v1/auth/groups"
        client.post(base, json={"name": "soc", "roles": []}, headers=admin_headers)
        _settled(app)

        resp = client.post(
            f"{base}/soc/members", json={"username": "viewer"}, headers=admin_headers
        )
        assert resp.status_code == 200, resp.text
        resp = client.delete(f"{base}/soc/members/viewer", headers=admin_headers)
        assert resp.status_code == 200, resp.text

        _settled(app)
        assert reconciles.calls == 1

    def test_scim_group_create_and_delete_each_reconcile(
        self, app, client, admin_headers, reconciles
    ):
        resp = client.post(
            f"{_SCIM}/Groups",
            json={"schemas": [_SCIM_GROUP], "displayName": "scim-team"},
            headers=admin_headers,
        )
        assert resp.status_code == 201, resp.text
        _settled(app)
        assert reconciles.calls == 1

        resp = client.delete(f"{_SCIM}/Groups/scim-team", headers=admin_headers)
        assert resp.status_code == 204, resp.text
        _settled(app)
        assert reconciles.calls == 2

    def test_an_oidc_sync_that_creates_groups_reconciles_once(
        self, app, client, admin_headers, reconciles, tmp_path, monkeypatch
    ):
        """The directory sync runs on the shipped offline directory backend."""
        from dfe_engine.auth.oidc.models import GroupResolutionConfig, OIDCProvider

        directory = tmp_path / "directory.json"
        directory.write_text(
            '{"groups": [{"id": "g1", "name": "soc"}, {"id": "g2", "name": "noc"}]}',
            encoding="utf-8",
        )
        monkeypatch.setenv("DFE_OIDC_MOCK_DIRECTORY", str(directory))
        app.state.oidc_provider_registry.create(
            "idp",
            OIDCProvider(groups=GroupResolutionConfig(mode="api", directory_backend="mock")),
        )
        url = "/api/v1/auth/oidc-providers/idp/sync"

        first = client.post(url, headers=admin_headers)
        assert first.status_code == 200, first.text
        assert first.json()["created"] == 2
        _settled(app)
        assert reconciles.calls == 1

        again = client.post(url, headers=admin_headers)
        assert again.json()["created"] == 0
        _settled(app)
        assert reconciles.calls == 1


def test_tenant_isolation_off_wires_no_trigger(api_settings, monkeypatch):
    """The reconcile is off with tenant isolation, and so is every reconcile after it."""
    from fastapi.testclient import TestClient

    from dfe_engine.api.app import create_app
    from dfe_engine.api.deps import _registries
    from dfe_engine.governance.ch.bootstrap import TENANT_ISOLATION_ENV

    monkeypatch.setenv(TENANT_ISOLATION_ENV, "false")
    application = create_app(settings=api_settings)
    try:
        with TestClient(application, raise_server_exceptions=False):
            assert getattr(application.state, "ch_rbac_reconcile", None) is None
    finally:
        _registries.clear()
