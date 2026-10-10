#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_role_write_reconciles.py
#  Purpose:      A role write asks for a CH RBAC reconcile, since scoped decides the CH users
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Creating, updating or deleting a role re-renders the groups' ClickHouse users.

A role's ``scoped`` flag decides whether a system group holding it is pinned to
its org, so a role write that waited for the next org or group change would
leave a group reading every org after its role was marked scoped. The app's
trigger is swapped for one whose reconcile counts its runs.
"""

import threading

import pytest

from dfe_engine.governance.ch import ReconcileResult
from tests.support.reconcile_trigger import swap_in_trigger

_WAIT = 10.0
_ROLES = "/api/v1/auth/roles"


class _Recorder:
    """A reconcile that counts its calls."""

    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self) -> ReconcileResult:
        with self._lock:
            self.calls += 1
        return ReconcileResult()


@pytest.fixture
def reconciles(app, client):
    recorder = _Recorder()
    swap_in_trigger(app, recorder, settle_seconds=0.2, retry_initial_seconds=0.2)
    return recorder


def _settled(app) -> None:
    assert app.state.ch_rbac_reconcile.wait_idle(_WAIT)


def test_create_update_and_delete_each_reconcile(app, client, admin_headers, reconciles):
    body = {"name": "tenant_viewer", "permissions": ["query:execute"], "scoped": False}
    created = client.post(_ROLES, json=body, headers=admin_headers)
    assert created.status_code == 201, created.text
    _settled(app)
    assert reconciles.calls == 1

    updated = client.put(f"{_ROLES}/tenant_viewer", json={"scoped": True}, headers=admin_headers)
    assert updated.status_code == 200, updated.text
    _settled(app)
    assert reconciles.calls == 2

    deleted = client.delete(f"{_ROLES}/tenant_viewer", headers=admin_headers)
    assert deleted.status_code == 204, deleted.text
    _settled(app)
    assert reconciles.calls == 3


def test_a_refused_create_does_not_reconcile(app, client, admin_headers, reconciles):
    body = {"name": "admin", "permissions": ["*"]}

    resp = client.post(_ROLES, json=body, headers=admin_headers)

    assert resp.status_code == 409, resp.text
    _settled(app)
    assert reconciles.calls == 0
