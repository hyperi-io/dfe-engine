"""GET/PUT /api/v1/gitops/auto-merge - the UI's auto-merge contract."""

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


class TestAutoMergeApi:
    def test_503_when_gitops_not_configured(self, client, admin_headers):
        resp = client.get("/api/v1/gitops/auto-merge", headers=admin_headers)
        assert resp.status_code == 503
        assert resp.json()["code"] == "not_configured"

    def test_get_default_prod_team(self, client, app, admin_headers, tmp_path):
        # test settings default: env=production, gitops.mode=team
        _wire_gitcrud(app, tmp_path)
        resp = client.get("/api/v1/gitops/auto-merge", headers=admin_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "stored": False,
            "effective": False,
            "allowed": False,
            "reason": body["reason"],
        }
        assert "DFE_GITOPS_MODE" in body["reason"]

    def test_enable_refused_in_prod_team(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        resp = client.put(
            "/api/v1/gitops/auto-merge", json={"enabled": True}, headers=admin_headers
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "auto_merge_forbidden"

    def test_enable_allowed_in_solo(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        app.state.settings.gitops.mode = "solo"
        resp = client.put(
            "/api/v1/gitops/auto-merge", json={"enabled": True}, headers=admin_headers
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["stored"] is True
        assert body["effective"] is True
        # the toggle is itself a gitops commit
        assert gc.get("gov_settings", "gitops")["auto_merge"] is True

    def test_disable_always_allowed(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        app.state.settings.gitops.mode = "solo"
        client.put("/api/v1/gitops/auto-merge", json={"enabled": True}, headers=admin_headers)
        # flip the deployment back to team: stored ON, gate refuses
        app.state.settings.gitops.mode = "team"
        got = client.get("/api/v1/gitops/auto-merge", headers=admin_headers)
        assert got.json()["stored"] is True
        assert got.json()["effective"] is False
        # turning OFF must still work
        off = client.put(
            "/api/v1/gitops/auto-merge", json={"enabled": False}, headers=admin_headers
        )
        assert off.status_code == 200
        assert off.json()["stored"] is False

    def test_viewer_cannot_toggle(self, client, app, viewer_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        app.state.settings.gitops.mode = "solo"
        resp = client.put(
            "/api/v1/gitops/auto-merge", json={"enabled": True}, headers=viewer_headers
        )
        assert resp.status_code == 403


class TestAutoMergedBadge:
    def _enable(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        app.state.settings.gitops.mode = "solo"
        resp = client.put(
            "/api/v1/gitops/auto-merge", json={"enabled": True}, headers=admin_headers
        )
        assert resp.status_code == 200
        return gc

    def test_helm_write_badged_in_prod_solo(self, client, app, admin_headers, tmp_path):
        # env=production -> helmvars write would be PR mode -> badge when ON
        self._enable(client, app, admin_headers, tmp_path)
        resp = client.put(
            "/api/v1/helm/files/receiver-default/vars/keda.maxReplicas",
            json={"value": 10},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["auto_merged"] is True

    def test_helm_write_not_badged_when_off(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        resp = client.put(
            "/api/v1/helm/files/receiver-default/vars/keda.maxReplicas",
            json={"value": 10},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["auto_merged"] is False

    def test_noop_write_not_badged_or_warned(self, client, app, admin_headers, tmp_path):
        # auto-merge effective-ON: a repeat write with the SAME value is a no-op
        # commit (res.changed False) - must not badge, must not WARN.
        self._enable(client, app, admin_headers, tmp_path)
        first = client.put(
            "/api/v1/helm/files/receiver-default/vars/keda.maxReplicas",
            json={"value": 10},
            headers=admin_headers,
        )
        assert first.status_code == 200, first.text
        second = client.put(
            "/api/v1/helm/files/receiver-default/vars/keda.maxReplicas",
            json={"value": 10},
            headers=admin_headers,
        )
        assert second.status_code == 200, second.text
        body = second.json()
        assert body["changed"] is False
        assert body["auto_merged"] is False

    def test_action_invoke_badged(self, client, app, admin_headers, tmp_path):
        # governance class always resolves to PR mode -> badge when ON
        self._enable(client, app, admin_headers, tmp_path)
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
        res = client.post("/api/v1/governance/actions/scale-receiver/invoke", headers=admin_headers)
        assert res.status_code == 200, res.text
        assert res.json()["auto_merged"] is True

    def test_create_action_succeeds_with_auto_merge_on(self, client, app, admin_headers, tmp_path):
        # spec: the conversion WARN extends to admin CRUD too - create_action still
        # succeeds (201) when auto-merge is ON and would otherwise have been PR-mode.
        self._enable(client, app, admin_headers, tmp_path)
        action = {
            "name": "scale-loader",
            "description": "scale loader",
            "required_action": "action:invoke:scale-loader",
            "changes": [
                {
                    "cls": "helmvars",
                    "name": "loader-default",
                    "path": "keda.maxReplicas",
                    "value": 9,
                }
            ],
        }
        created = client.post(
            "/api/v1/governance/admin/actions", json=action, headers=admin_headers
        )
        assert created.status_code == 201, created.text
