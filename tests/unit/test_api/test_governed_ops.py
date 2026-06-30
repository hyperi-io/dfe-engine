"""Tests for the Governed Ops routers (Tier-1 helm vars + Tier-2 actions)."""

from __future__ import annotations

import pytest

from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance import PolicyStore


def _wire_gitcrud(app, tmp_path):
    """Attach a real local-repo GitCrud to the running app (gitops off by default)."""
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    gc = GitCrud(repo, default_registry())
    app.state.gitcrud = gc
    app.state.policy_store = PolicyStore(gc)
    return gc


class TestTier1HelmVars:
    def test_503_when_gitops_not_configured(self, client, admin_headers):
        # default test settings have gitops disabled -> gitcrud is None
        resp = client.get("/api/v1/helm/files", headers=admin_headers)
        assert resp.status_code == 503
        assert resp.json()["code"] == "not_configured"

    def test_set_then_list_var(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        # KEDA bound is fine; replicaCount would be rejected (controller-owned)
        resp = client.put(
            "/api/v1/helm/files/receiver-default/vars/keda.maxReplicas",
            json={"value": 10},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["changed"] is True
        assert resp.json()["commit_sha"]

        listed = client.get("/api/v1/helm/files/receiver-default/vars", headers=admin_headers)
        assert listed.status_code == 200
        by_path = {v["path"]: v for v in listed.json()}
        assert by_path["keda.maxReplicas"]["value"] == 10

        files = client.get("/api/v1/helm/files", headers=admin_headers)
        assert files.json() == ["receiver-default"]

    def test_replicacount_rejected(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        resp = client.put(
            "/api/v1/helm/files/receiver-default/vars/replicaCount",
            json={"value": 3},
            headers=admin_headers,
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "policy_violation"

    def test_viewer_cannot_write(self, client, app, viewer_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        resp = client.put(
            "/api/v1/helm/files/receiver-default/vars/keda.maxReplicas",
            json={"value": 10},
            headers=viewer_headers,
        )
        assert resp.status_code == 403


class TestTier2Actions:
    def test_create_then_invoke_action(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        action = {
            "name": "scale-receiver",
            "description": "scale receiver",
            "required_action": "action:invoke:scale-receiver",
            "changes": [
                {
                    "cls": "helmvars",
                    "name": "receiver-default",
                    "path": "keda.maxReplicas",
                    "value": 12,
                }
            ],
        }
        created = client.post(
            "/api/v1/governance/admin/actions", json=action, headers=admin_headers
        )
        assert created.status_code == 201, created.text

        listed = client.get("/api/v1/governance/actions", headers=admin_headers)
        assert listed.json() == ["scale-receiver"]

        # dry-run first - no commit
        dry = client.post(
            "/api/v1/governance/actions/scale-receiver/invoke",
            params={"dry_run": True},
            headers=admin_headers,
        )
        assert dry.status_code == 200, dry.text
        assert dry.json()["dry_run"] is True
        assert dry.json()["commit_sha"] is None

        # real invoke - one commit, value applied
        res = client.post(
            "/api/v1/governance/actions/scale-receiver/invoke",
            headers=admin_headers,
        )
        assert res.status_code == 200, res.text
        assert res.json()["changed"] is True
        assert res.json()["commit_sha"]

        crud = app.state.gitcrud
        assert crud.get("helmvars", "receiver-default")["keda"]["maxReplicas"] == 12

    def test_invoke_missing_action_404(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        resp = client.post("/api/v1/governance/actions/nope/invoke", headers=admin_headers)
        assert resp.status_code == 404


@pytest.mark.parametrize("path", ["/api/v1/governance/actions", "/api/v1/helm/files"])
def test_governed_ops_requires_auth(client, path):
    # no auth header -> 401
    assert client.get(path).status_code == 401
