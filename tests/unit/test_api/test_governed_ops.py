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


def _scoped_headers(app, api_settings, *, username, role, permissions):
    """Create a role+group+account with exactly these perms; return auth headers."""
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


def _action_doc():
    return {
        "name": "scale-receiver",
        "description": "scale",
        "required_action": "action:invoke:scale-receiver",
        "changes": [
            {"cls": "helmvars", "name": "receiver-default", "path": "keda.maxReplicas", "value": 9}
        ],
    }


class TestPerActionRBAC:
    def test_reader_can_list_but_not_invoke(
        self, client, app, api_settings, admin_headers, tmp_path
    ):
        _wire_gitcrud(app, tmp_path)
        client.post("/api/v1/governance/admin/actions", json=_action_doc(), headers=admin_headers)
        reader = _scoped_headers(
            app,
            api_settings,
            username="govreader",
            role="gov-reader",
            permissions=["governance:read"],
        )
        # can see actions (governance:read)
        assert client.get("/api/v1/governance/actions", headers=reader).status_code == 200
        # cannot invoke (lacks action:invoke:scale-receiver) -> 403
        denied = client.post("/api/v1/governance/actions/scale-receiver/invoke", headers=reader)
        assert denied.status_code == 403

    def test_scoped_invoker_can_invoke(self, client, app, api_settings, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        client.post("/api/v1/governance/admin/actions", json=_action_doc(), headers=admin_headers)
        scaler = _scoped_headers(
            app,
            api_settings,
            username="scaler",
            role="scaler",
            permissions=["governance:read", "action:invoke:scale-receiver"],
        )
        ok = client.post("/api/v1/governance/actions/scale-receiver/invoke", headers=scaler)
        assert ok.status_code == 200, ok.text
        assert ok.json()["changed"] is True


class TestRouterConcurrency:
    def test_stale_if_match_returns_409(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        url = "/api/v1/helm/files/receiver-default/vars/keda.maxReplicas"
        r1 = client.put(url, json={"value": 1}, headers=admin_headers)
        sha1 = r1.json()["commit_sha"]
        # advance HEAD with a fresh write
        client.put(url, json={"value": 2}, headers=admin_headers)
        # now sha1 is stale
        conflict = client.put(url, json={"value": 3}, headers={**admin_headers, "If-Match": sha1})
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "conflict"
        assert "current" in conflict.json()["context"]


class TestRouterProtected:
    def test_writer_without_override_blocked_by_policy(
        self, client, app, api_settings, admin_headers, tmp_path
    ):
        _wire_gitcrud(app, tmp_path)
        # admin defines a protected-var policy
        client.post(
            "/api/v1/governance/admin/policies",
            json={"name": "lock", "protected": ["helmvars:*:keda.maxReplicas"]},
            headers=admin_headers,
        )
        writer = _scoped_headers(
            app,
            api_settings,
            username="writer",
            role="helm-writer",
            permissions=["helmvars:read", "helmvars:write"],  # NO helmvars:override
        )
        resp = client.put(
            "/api/v1/helm/files/receiver-default/vars/keda.maxReplicas",
            json={"value": 5},
            headers=writer,
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "protected_var"

    def test_admin_override_bypasses_policy(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        client.post(
            "/api/v1/governance/admin/policies",
            json={"name": "lock", "protected": ["helmvars:*:keda.maxReplicas"]},
            headers=admin_headers,
        )
        # admin has '*' -> helmvars:override -> allowed through
        resp = client.put(
            "/api/v1/helm/files/receiver-default/vars/keda.maxReplicas",
            json={"value": 5},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text


def _latest_entry(gc):
    from dfe_engine.gitcrud.log import read_log

    return read_log(gc, limit=1)[0][0]


class TestHelmMessageBudget:
    """FIX 2: build_message budget-truncates instead of 500ing an over-long subject."""

    def test_long_file_and_leaf_put_returns_200_with_fitting_subject(
        self, client, app, admin_headers, tmp_path
    ):
        gc = _wire_gitcrud(app, tmp_path)
        # 'cfg(receiver-default): set clickhouse_max_connections' is 53 chars raw
        resp = client.put(
            "/api/v1/helm/files/receiver-default/vars/clickhouse_max_connections",
            json={"value": 512},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        e = _latest_entry(gc)
        subject = f"{e.ctype}({e.scope}): {e.summary}"
        assert len(subject) <= 50
        assert e.conforming is True
        assert e.actor == "admin"

    def test_non_ascii_var_maps_to_422_not_500(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        # a non-ASCII leaf makes the rendered subject non-ASCII -> build_message
        # raises CommitPolicyError, which the handler maps to 422 (never 500).
        resp = client.put(
            "/api/v1/helm/files/receiver-default/vars/naïve",
            json={"value": 1},
            headers=admin_headers,
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "invalid_commit_message"


class TestDeleteVarParity:
    """FIX 3: DELETE (revert) honours the SAME protected-var policy as PUT."""

    def test_delete_protected_var_blocked_without_override(
        self, client, app, api_settings, admin_headers, tmp_path
    ):
        _wire_gitcrud(app, tmp_path)
        client.post(
            "/api/v1/governance/admin/policies",
            json={"name": "lock", "protected": ["helmvars:*:keda.maxReplicas"]},
            headers=admin_headers,
        )
        # admin (has override) seeds the var
        client.put(
            "/api/v1/helm/files/receiver-default/vars/keda.maxReplicas",
            json={"value": 5},
            headers=admin_headers,
        )
        writer = _scoped_headers(
            app,
            api_settings,
            username="delwriter",
            role="del-writer",
            permissions=["helmvars:read", "helmvars:write"],  # NO override
        )
        blocked = client.delete(
            "/api/v1/helm/files/receiver-default/vars/keda.maxReplicas", headers=writer
        )
        assert blocked.status_code == 403
        assert blocked.json()["code"] == "protected_var"
        # admin override CAN revert it
        ok = client.delete(
            "/api/v1/helm/files/receiver-default/vars/keda.maxReplicas", headers=admin_headers
        )
        assert ok.status_code == 200, ok.text


class TestHelmErrorMapping:
    """FIX 4: bad list index -> 422; unknown resource on GET/DELETE -> 404."""

    def test_bad_list_index_returns_422(self, client, app, admin_headers, tmp_path):
        gc = _wire_gitcrud(app, tmp_path)
        gc.put("helmvars", "receiver-default", {"tolerations": [{"key": "a"}]}, actor="seed")
        resp = client.put(
            "/api/v1/helm/files/receiver-default/vars/tolerations[5].key",
            json={"value": "x"},
            headers=admin_headers,
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "invalid_path"

    def test_unknown_file_get_vars_returns_404(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        resp = client.get("/api/v1/helm/files/nonexistent/vars", headers=admin_headers)
        assert resp.status_code == 404

    def test_unknown_file_delete_var_returns_404(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        resp = client.delete("/api/v1/helm/files/nonexistent/vars/some.path", headers=admin_headers)
        assert resp.status_code == 404


class TestInvokeActionAudit:
    """FIX 5a: invoke_action uses check_action -> emits the permission-denied audit."""

    def test_denied_invoke_emits_audit_event(
        self, client, app, api_settings, admin_headers, tmp_path, monkeypatch
    ):
        _wire_gitcrud(app, tmp_path)
        client.post("/api/v1/governance/admin/actions", json=_action_doc(), headers=admin_headers)
        reader = _scoped_headers(
            app, api_settings, username="ro", role="ro-only", permissions=["governance:read"]
        )
        import dfe_engine.api.deps as deps

        calls: list = []
        monkeypatch.setattr(deps, "audit_permission_denied", lambda *a, **k: calls.append(a))
        denied = client.post("/api/v1/governance/actions/scale-receiver/invoke", headers=reader)
        assert denied.status_code == 403
        # the bare authorize() the old code used never emitted this; check_action does
        assert calls


class TestChRbacReconcileEndpoint:
    def test_requires_auth(self, client):
        # the governance:write gate rejects before any ClickHouse work
        assert client.post("/api/v1/governance/ch-rbac/reconcile").status_code == 401

    def test_viewer_forbidden(self, client, viewer_headers):
        resp = client.post("/api/v1/governance/ch-rbac/reconcile", headers=viewer_headers)
        assert resp.status_code == 403

    def test_reconcile_uses_app_state_settings_not_env(
        self, client, app, admin_headers, monkeypatch
    ):
        # FIX 5b: the endpoint must read app.state.settings, NOT re-load from env.
        from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager

        app.state.settings.clickhouse.host = "app-state-ch-sentinel"
        captured: dict = {}

        def _capture(cfg):
            captured["host"] = cfg["ch_host"]
            raise ConnectionError("ch down")  # force the 503 branch

        monkeypatch.setattr(ClickHouseManager, "get_instance", staticmethod(_capture))
        resp = client.post("/api/v1/governance/ch-rbac/reconcile", headers=admin_headers)
        assert resp.status_code == 503
        # a fresh load_settings() (the old bug) would NOT see the app-state mutation
        assert captured["host"] == "app-state-ch-sentinel"


class TestGovernedCli:
    """FIX 6: the governed-ops CLI applies the SAME guards as the API + non-zero exits."""

    def _seed_repo_env(self, tmp_path, monkeypatch):
        from dfe_engine.gitcrud import GitCrud, default_registry
        from dfe_engine.gitops.repo import GitopsRepo

        repo = tmp_path / "deploy"
        monkeypatch.setenv("DFE_GITOPS_ENABLED", "true")
        monkeypatch.setenv("DFE_GITOPS_LOCAL_PATH", str(repo))
        monkeypatch.setenv("DFE_GITOPS_PUSH", "false")
        return GitCrud(GitopsRepo(local_path=str(repo), push=False), default_registry())

    def test_cli_helm_set_refuses_protected_var(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner

        from dfe_engine.cli.governed_ops import governed_app

        gc = self._seed_repo_env(tmp_path, monkeypatch)
        gc.put(
            "policies",
            "lock",
            {"name": "lock", "protected": ["helmvars:*:keda.maxReplicas"]},
            actor="admin",
        )
        runner = CliRunner()
        blocked = runner.invoke(
            governed_app, ["helm", "set", "receiver-default", "keda.maxReplicas", "5"]
        )
        assert blocked.exit_code == 1
        forced = runner.invoke(
            governed_app,
            ["helm", "set", "receiver-default", "keda.maxReplicas", "5", "--override"],
        )
        assert forced.exit_code == 0, forced.output

    def test_cli_helm_set_rejects_controller_owned_var(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner

        from dfe_engine.cli.governed_ops import governed_app

        self._seed_repo_env(tmp_path, monkeypatch)
        runner = CliRunner()
        res = runner.invoke(governed_app, ["helm", "set", "receiver-default", "replicaCount", "3"])
        assert res.exit_code == 1  # validate_change rejects (same as API 403)

    def test_cli_reconcile_exits_nonzero_on_errors(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        from typer.testing import CliRunner

        import dfe_engine.governance.ch as ch_mod
        import dfe_engine.secrets as secrets_mod
        from dfe_engine.cli.governed_ops import governed_app
        from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager

        # CH is unavailable in unit tests: substitute the client boundary + secrets
        # store, and feed a reconcile result carrying an error, to exercise ONLY the
        # CLI's exit-code contract (partial reconcile must not exit 0).
        client_stub = SimpleNamespace(
            get_clickhouse_client=lambda: SimpleNamespace(_client=object())
        )
        monkeypatch.setattr(
            ClickHouseManager, "get_instance", staticmethod(lambda cfg: client_stub)
        )
        monkeypatch.setattr(secrets_mod, "build_secrets", lambda cfg: None)
        monkeypatch.setattr(
            ch_mod,
            "reconcile_ch_rbac",
            lambda *a, **k: SimpleNamespace(statements=[], dropped=[], minted=[], errors=["boom"]),
        )
        result = CliRunner().invoke(governed_app, ["reconcile-ch-rbac"])
        assert result.exit_code == 1
