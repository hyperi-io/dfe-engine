"""GET /api/v1/gitops/log - flat + grouped audit log."""

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


class TestGitopsLogApi:
    def test_503_when_not_configured(self, client, admin_headers):
        resp = client.get("/api/v1/gitops/log", headers=admin_headers)
        assert resp.status_code == 503

    def test_flat_log_after_writes(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        put = client.put(
            "/api/v1/helm/files/receiver-default/vars/keda.maxReplicas",
            json={"value": 10},
            headers=admin_headers,
        )
        assert put.status_code == 200
        resp = client.get("/api/v1/gitops/log", headers=admin_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["next_before"] is None
        assert len(body["entries"]) == 1
        entry = body["entries"][0]
        assert entry["scope"] == "receiver-default"
        assert entry["ctype"] == "cfg"
        assert entry["conforming"] is True
        assert entry["state"] == "committed"
        assert entry["resources"] == ["helmvars/receiver-default"]

    def test_grouped_log(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        for path in ("keda.maxReplicas", "keda.minReplicas"):
            client.put(
                f"/api/v1/helm/files/receiver-default/vars/{path}",
                json={"value": 3},
                headers=admin_headers,
            )
        resp = client.get(
            "/api/v1/gitops/log",
            params={"group_by": "scope"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        groups = resp.json()["groups"]
        assert len(groups) == 1
        assert groups[0]["key"] == "receiver-default"
        assert groups[0]["count"] == 2
        assert groups[0]["latest"]["ctype"] == "cfg"

    def test_bad_group_by_rejected(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        resp = client.get(
            "/api/v1/gitops/log",
            params={"group_by": "nope"},
            headers=admin_headers,
        )
        assert resp.status_code == 422

    def test_log_read_is_offloaded_off_the_event_loop(
        self, client, app, admin_headers, tmp_path, monkeypatch
    ):
        # SYNC-IN-ASYNC: the blocking full-history walk must run via asyncio.to_thread
        # so it does not stall the single-worker event loop.
        _wire_gitcrud(app, tmp_path)
        client.put(
            "/api/v1/helm/files/receiver-default/vars/keda.maxReplicas",
            json={"value": 3},
            headers=admin_headers,
        )
        import dfe_engine.api.v1.gitops as gitops_mod

        offloaded: list = []
        real = gitops_mod.asyncio.to_thread

        async def _spy(fn, *a, **k):
            offloaded.append(getattr(fn, "__name__", ""))
            return await real(fn, *a, **k)

        monkeypatch.setattr(gitops_mod.asyncio, "to_thread", _spy)
        resp = client.get("/api/v1/gitops/log", headers=admin_headers)
        assert resp.status_code == 200
        assert "read_log" in offloaded

    def test_unknown_cursor_rejected(self, client, app, admin_headers, tmp_path):
        _wire_gitcrud(app, tmp_path)
        client.put(
            "/api/v1/helm/files/receiver-default/vars/keda.maxReplicas",
            json={"value": 10},
            headers=admin_headers,
        )
        resp = client.get(
            "/api/v1/gitops/log",
            params={"before": "deadbeef" * 5},
            headers=admin_headers,
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "unknown_cursor"
