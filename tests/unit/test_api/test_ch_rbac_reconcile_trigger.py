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
import time

import pytest

from dfe_engine.governance.ch import ReconcileResult
from tests.support.loopback import CountingListener
from tests.support.reconcile_trigger import swap_in_trigger

_WAIT = 10.0
_SCIM = "/api/v1/scim/v2"
_SCIM_GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"


class _Recorder:
    """A reconcile that counts its calls, and fails the next ``failures`` of them."""

    def __init__(self) -> None:
        self.calls = 0
        self.failures = 0
        self._lock = threading.Lock()

    def __call__(self) -> ReconcileResult:
        with self._lock:
            self.calls += 1
            failing = self.failures > 0
            self.failures -= int(failing)
        if failing:
            raise ConnectionRefusedError("clickhouse is down")
        return ReconcileResult()


@pytest.fixture
def reconciles(app, client):
    """The app's trigger, reconciling into a recorder; settles long enough for a burst."""
    recorder = _Recorder()
    swap_in_trigger(app, recorder, settle_seconds=1.0, retry_initial_seconds=0.2)
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

    def test_an_org_created_while_clickhouse_is_down_is_provisioned_when_it_is_back(
        self, app, client, admin_headers, reconciles
    ):
        """The create answers 201 at once; the reconcile retries on its own, no second write."""
        reconciles.failures = 2

        resp = client.post("/api/v1/orgs", json={"name": "acme"}, headers=admin_headers)

        assert resp.status_code == 201, resp.text
        assert app.state.org_registry.get("acme") is not None
        _settled(app)
        assert reconciles.calls == 3

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


def _failed_runs(manager) -> float:
    from prometheus_client.parser import text_string_to_metric_families

    from dfe_engine.governance.ch.trigger import RECONCILES

    for family in text_string_to_metric_families(manager.metrics_text):
        for sample in family.samples:
            if sample.name == RECONCILES and sample.labels.get("outcome") == "failed":
                return sample.value
    return 0.0


def test_a_unit_test_app_never_reconciles_against_a_listening_clickhouse(api_settings):
    """Startup and an org write both reconcile; neither may dial the configured ClickHouse.

    A developer's local stack publishes its ClickHouse on the default address, and
    the reconcile drops users there. The conftest guard is what keeps a unit run
    off it, so this fails the moment a reconcile reaches a live address.
    """
    from fastapi.testclient import TestClient
    from scalo.metrics import create_metrics

    from dfe_engine.api.app import create_app
    from dfe_engine.api.deps import _registries
    from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager

    # Each connection is closed at once, so a reconcile that does dial fails fast.
    clickhouse = CountingListener()
    api_settings.clickhouse.host = "127.0.0.1"
    api_settings.clickhouse.port = clickhouse.port
    api_settings.clickhouse.secure = False
    # The manager is a process singleton, so one another test built would point elsewhere.
    ClickHouseManager.reset_instance()
    manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
    application = create_app(settings=api_settings, metrics_manager=manager)
    try:
        with TestClient(application, raise_server_exceptions=False):
            trigger = application.state.ch_rbac_reconcile
            assert trigger is not None
            application.state.org_registry.create("acme")
            deadline = time.monotonic() + _WAIT
            while _failed_runs(manager) < 1 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert _failed_runs(manager) >= 1
    finally:
        _registries.clear()
        ClickHouseManager.reset_instance()
        clickhouse.close()

    assert clickhouse.connections == 0


def test_tenant_isolation_off_wires_no_change_trigger(api_settings, monkeypatch):
    """With tenant isolation off an org or group change reconciles nothing."""
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


@pytest.mark.parametrize(
    ("isolation", "attribute"),
    [("true", "ch_rbac_reconcile"), ("false", "ch_service_role_reconcile")],
    ids=["isolation-on", "isolation-off"],
)
def test_a_startup_reconcile_that_fails_is_left_retrying_and_the_api_still_starts(
    api_settings, monkeypatch, isolation, attribute
):
    """ClickHouse down at boot: the API serves, and a retry is waiting to make the users.

    The conftest guard refuses the admin client, which is how the startup run sees
    a ClickHouse that is down. The retry runs on the trigger's own back-off, so the
    test reads that one is pending rather than waiting it out.
    """
    from fastapi.testclient import TestClient
    from scalo.metrics import create_metrics

    from dfe_engine.api.app import create_app
    from dfe_engine.api.deps import _registries
    from dfe_engine.governance.ch.bootstrap import TENANT_ISOLATION_ENV

    monkeypatch.setenv(TENANT_ISOLATION_ENV, isolation)
    manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
    application = create_app(settings=api_settings, metrics_manager=manager)
    try:
        with TestClient(application, raise_server_exceptions=False) as client:
            trigger = getattr(application.state, attribute)
            assert _failed_runs(manager) == 1
            assert trigger.wait_idle(0.0) is False, "no retry is waiting behind the failed run"
            assert client.get("/livez").status_code == 200
        assert trigger.wait_idle(0.0), "shutdown left the retry running"
    finally:
        _registries.clear()
