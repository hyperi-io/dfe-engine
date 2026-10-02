"""Tests for the Governed Ops routers (Tier-1 helm vars + Tier-2 actions)."""

import pytest

from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance import PolicyStore


def _wire_gitcrud(app, tmp_path):
    """Attach a real local-repo GitCrud to the running app (gitops off by default).

    These exercise CRUD / RBAC / concurrency / protected-var mechanics, which are
    posture-independent, so run them under a dev posture where direct-commit is the
    sanctioned path. Production+team now routes governed writes to a review PR -
    that enforcement has its own tests in test_auto_merge_api.py.
    """
    app.state.settings.env = "dev"
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

    def test_create_then_list_and_get_policy(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        body = {
            "name": "lock",
            "description": "lock keda max",
            "protected": ["helmvars:*:keda.maxReplicas"],
        }
        created = client.post("/api/v1/governance/admin/policies", json=body, headers=admin_headers)
        assert created.status_code == 201, created.text

        listed = client.get("/api/v1/governance/policies", headers=admin_headers)
        assert listed.status_code == 200
        assert listed.json() == ["lock"]

        detail = client.get("/api/v1/governance/policies/lock", headers=admin_headers)
        assert detail.status_code == 200
        assert detail.json()["protected"] == body["protected"]

    def test_get_missing_policy_404(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        resp = client.get("/api/v1/governance/policies/nope", headers=admin_headers)
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

    def test_the_parent_of_a_locked_var_is_refused_for_set_and_revert(
        self, client, app, api_settings, admin_headers, tmp_path
    ):
        """A map at keda replaces keda.maxReplicas, and a revert of keda removes it."""
        gc = _wire_gitcrud(app, tmp_path)
        stored = {"keda": {"maxReplicas": 4, "minReplicas": 1}}
        gc.put("helmvars", "receiver-default", stored, "tester")
        client.post(
            "/api/v1/governance/admin/policies",
            json={"name": "lock", "protected": ["helmvars:*:keda.maxReplicas"]},
            headers=admin_headers,
        )
        writer = _scoped_headers(
            app,
            api_settings,
            username="mapwriter",
            role="helm-map-writer",
            permissions=["helmvars:read", "helmvars:write"],  # NO helmvars:override
        )
        url = "/api/v1/helm/files/receiver-default/vars/keda"
        put = client.put(url, json={"value": {"minReplicas": 1}}, headers=writer)
        assert put.status_code == 403, put.text
        assert put.json()["code"] == "protected_var"
        deleted = client.delete(url, headers=writer)
        assert deleted.status_code == 403, deleted.text
        assert gc.get("helmvars", "receiver-default") == stored

        sibling = client.put(f"{url}.minReplicas", json={"value": 2}, headers=writer)
        assert sibling.status_code == 200, sibling.text

    def test_the_override_lifts_the_lock_but_not_a_value_rule_inside_a_map(
        self, client, app, admin_headers, tmp_path
    ):
        """The grant unlocks image.tag; a floating tag nested in the image map stays refused."""
        gc = _wire_gitcrud(app, tmp_path)
        stored = {"image": {"repository": "ghcr.io/x/y", "tag": "v1.0.0"}}
        gc.put("helmvars", "receiver-default", stored, "tester")
        client.post(
            "/api/v1/governance/admin/policies",
            json={"name": "lock", "protected": ["helmvars:*:image.tag"]},
            headers=admin_headers,
        )
        url = "/api/v1/helm/files/receiver-default/vars/image"
        # admin holds '*', so helmvars:override
        floating = {"repository": "ghcr.io/x/y", "tag": "latest"}
        refused = client.put(url, json={"value": floating}, headers=admin_headers)
        assert refused.status_code == 403, refused.text
        assert refused.json()["code"] == "policy_violation"
        assert "image.tag" in refused.json()["message"]
        assert gc.get("helmvars", "receiver-default") == stored

        pinned = {"repository": "ghcr.io/x/y", "tag": "v1.1.0"}
        allowed = client.put(url, json={"value": pinned}, headers=admin_headers)
        assert allowed.status_code == 200, allowed.text
        assert gc.get("helmvars", "receiver-default") == {"image": pinned}

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            ("image", {"tag": "latest"}),
            ("deploy", {"replicaCount": 3}),
            ("app", {"sidecar": {"image": {"tag": "latest"}}}),
        ],
    )
    def test_a_map_cannot_carry_a_refused_leaf_past_the_value_rules(
        self, client, app, admin_headers, tmp_path, path, value
    ):
        gc = _wire_gitcrud(app, tmp_path)
        resp = client.put(
            f"/api/v1/helm/files/receiver-default/vars/{path}",
            json={"value": value},
            headers=admin_headers,
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["code"] == "policy_violation"
        assert gc.list("helmvars") == []

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


class TestChRbacReconcileEndpoint:
    def test_requires_auth(self, client):
        # the governance:write gate rejects before any ClickHouse work
        assert client.post("/api/v1/governance/ch-rbac/reconcile").status_code == 401

    def test_viewer_forbidden(self, client, viewer_headers):
        resp = client.post("/api/v1/governance/ch-rbac/reconcile", headers=viewer_headers)
        assert resp.status_code == 403
