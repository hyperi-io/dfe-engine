#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_argo_refresh_on_deploy.py
#  Purpose:      A source deploy has Argo CD refresh the Applications it pushed to
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A deploy that writes an app's overlay asks Argo CD to re-read the repository for that app.

Real local deploy repo, real API. The cluster call is recorded, so what is pinned is
which Applications are named and that nothing is sent off Kubernetes.
"""

import pytest

from dfe_engine.appmgmt import argo_refresh
from tests.unit.test_api.test_app_contracts import _deploy_vrl, _wire


def _set_state(client, headers, state: str) -> None:
    resp = client.patch("/api/v1/sources/edge", json={"state": state}, headers=headers)
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    ("target", "refreshed"), [("kubernetes", True), ("docker", False), ("unknown", False)]
)
def test_a_pass_refreshes_the_applications_it_pushed_only_on_kubernetes(
    client, app, admin_headers, tmp_path, monkeypatch, target, refreshed
):
    """Bringing the instance back writes its overlay, and removing it writes none to refresh."""
    _wire(app, tmp_path)
    assert _deploy_vrl(client, admin_headers, "edge").status_code == 200
    app.state.settings.deployment.target = target
    asked: list[list[str]] = []
    monkeypatch.setattr(
        argo_refresh, "refresh_applications", lambda names, **_kw: asked.append(list(names)) or []
    )

    _set_state(client, admin_headers, "dormant")
    assert asked == []

    _set_state(client, admin_headers, "active")
    assert asked == ([["dfe-transform-vrl-edge"]] if refreshed else [])


def test_a_refresh_the_cluster_refuses_does_not_fail_the_pass(
    client, app, admin_headers, tmp_path, monkeypatch
):
    _wire(app, tmp_path)
    assert _deploy_vrl(client, admin_headers, "edge").status_code == 200
    monkeypatch.setattr(argo_refresh, "refresh_applications", lambda names, **_kw: list(names))

    _set_state(client, admin_headers, "dormant")
    _set_state(client, admin_headers, "active")
