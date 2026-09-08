#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_source_apps_reconcile.py
#  Purpose:      Tests that a source write brings the deployed apps into step
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Every source write ends by reconciling the deploy repo with the sources.

The regression these guard: a source deployed with a match rule that the
receiver never received, because nothing pushed the compiled routing into the
overlay Argo applies.
"""

from __future__ import annotations

from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance import PolicyStore

RECEIVER_OVERLAY = "dfe-receiver-main-values"
FETCHER = "dfe-fetcher"


def _wire(app, tmp_path):
    """Attach a real local deploy repo with a deployed receiver."""
    app.state.settings.env = "dev"
    app.state.settings.deployment.target = "kubernetes"
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    gc = GitCrud(repo, default_registry())
    app.state.gitcrud = gc
    app.state.policy_store = PolicyStore(gc)
    gc.put(
        "helmvars",
        RECEIVER_OVERLAY,
        {"deploy": {"service": "dfe-receiver", "instance": "main"}},
        "test",
        message="test: deploy receiver",
    )
    return gc


def _receiver_body(name: str) -> dict:
    return {"source": name, "match": {"field": "_json.app", "value": name}}


def _fetcher_body(name: str, **fetcher) -> dict:
    return {
        "source": name,
        "fetcher": {
            "source_type": "crates_io",
            "config": {"crates": ["dfe-fetcher"]},
            **fetcher,
        },
    }


def _rules(gc) -> list[str]:
    doc = gc.get("helmvars", RECEIVER_OVERLAY)
    return [r["source"] for r in doc.get("config", {}).get("routing", {}).get("source_rules", [])]


class TestReceiverRoutingFollowsTheSources:
    def test_creating_a_source_delivers_its_rule(self, client, app, admin_headers, tmp_path):
        gc = _wire(app, tmp_path)

        resp = client.post("/api/v1/sources", json=_receiver_body("kvproof"), headers=admin_headers)

        assert resp.status_code == 201, resp.text
        assert resp.json()["apps_synced"] == ["dfe-receiver/main: sync routing"]
        assert resp.json()["apps_sync_error"] is None
        assert _rules(gc) == ["kvproof"]

    def test_disabling_a_source_withdraws_its_rule(self, client, app, admin_headers, tmp_path):
        gc = _wire(app, tmp_path)
        client.post("/api/v1/sources", json=_receiver_body("kvproof"), headers=admin_headers)

        resp = client.patch(
            "/api/v1/sources/kvproof", json={"state": "disabled"}, headers=admin_headers
        )

        assert resp.status_code == 200
        assert resp.json()["apps_synced"] == ["dfe-receiver/main: sync routing"]
        assert _rules(gc) == []

    def test_deleting_a_source_withdraws_its_rule(self, client, app, admin_headers, tmp_path):
        gc = _wire(app, tmp_path)
        client.post("/api/v1/sources", json=_receiver_body("kvproof"), headers=admin_headers)

        assert client.delete("/api/v1/sources/kvproof", headers=admin_headers).status_code == 204
        assert _rules(gc) == []

    def test_an_unchanged_write_is_not_a_commit(self, client, app, admin_headers, tmp_path):
        gc = _wire(app, tmp_path)
        client.post("/api/v1/sources", json=_receiver_body("kvproof"), headers=admin_headers)
        head = gc.head_revision()

        resp = client.put(
            "/api/v1/sources/kvproof",
            json={**_receiver_body("kvproof"), "description": "renamed"},
            headers=admin_headers,
        )

        assert resp.status_code == 200
        assert resp.json()["apps_synced"] == []
        assert gc.head_revision() == head

    def test_without_a_deploy_repo_the_write_still_lands(self, client, admin_headers):
        resp = client.post("/api/v1/sources", json=_receiver_body("kvproof"), headers=admin_headers)
        assert resp.status_code == 201
        assert resp.json()["apps_synced"] == []
        assert resp.json()["apps_sync_error"] is None


class TestFetcherInstancesFollowTheSources:
    def test_a_fetcher_source_gets_its_rule_but_no_instance_before_deploy(
        self, client, app, admin_headers, tmp_path
    ):
        gc = _wire(app, tmp_path)

        resp = client.post("/api/v1/sources", json=_fetcher_body("crates"), headers=admin_headers)

        assert resp.status_code == 201, resp.text
        assert _rules(gc) == ["crates"]
        assert "dfe-fetcher-crates-values" not in gc.list("helmvars")

    def test_a_deployed_fetcher_source_gets_its_instance(
        self, client, app, admin_headers, tmp_path
    ):
        from dfe_engine.api.deps import _registries

        gc = _wire(app, tmp_path)
        client.post("/api/v1/sources", json=_fetcher_body("crates"), headers=admin_headers)
        # The deploy endpoint needs ClickHouse; marking the version deployed is
        # the part of it the reconcile reads.
        _registries["source"].set_deployed_version("crates", "1.0.0")

        resp = client.post("/api/v1/sources/reconcile-apps", headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["changes"] == [f"{FETCHER}/crates: deploy instance"]
        doc = gc.get("helmvars", "dfe-fetcher-crates-values")
        assert doc["config"]["sources"]["crates_io"]["topic"] == "crates"

    def test_disabling_removes_a_deployed_instance(self, client, app, admin_headers, tmp_path):
        from dfe_engine.api.deps import _registries

        gc = _wire(app, tmp_path)
        client.post("/api/v1/sources", json=_fetcher_body("crates"), headers=admin_headers)
        _registries["source"].set_deployed_version("crates", "1.0.0")
        assert (
            client.post("/api/v1/sources/reconcile-apps", headers=admin_headers).status_code == 200
        )
        assert "dfe-fetcher-crates-values" in gc.list("helmvars")

        resp = client.patch(
            "/api/v1/sources/crates", json={"state": "disabled"}, headers=admin_headers
        )

        assert resp.status_code == 200
        assert "dfe-fetcher/crates: undeploy instance" in resp.json()["apps_synced"]
        assert "dfe-fetcher-crates-values" not in gc.list("helmvars")
        assert _rules(gc) == []

    def test_reconcile_apps_reports_what_it_wrote(self, client, app, admin_headers, tmp_path):
        gc = _wire(app, tmp_path)
        client.post("/api/v1/sources", json=_receiver_body("kvproof"), headers=admin_headers)
        # A hand edit over the derived block is drift the reconcile undoes.
        doc = gc.get("helmvars", RECEIVER_OVERLAY)
        doc["config"]["routing"]["source_rules"] = []
        gc.put("helmvars", RECEIVER_OVERLAY, doc, "test", message="test: hand edit")

        resp = client.post("/api/v1/sources/reconcile-apps", headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"changes": ["dfe-receiver/main: sync routing"]}
        assert _rules(gc) == ["kvproof"]

    def test_reconcile_apps_without_a_deploy_repo_is_503(self, client, admin_headers):
        resp = client.post("/api/v1/sources/reconcile-apps", headers=admin_headers)
        assert resp.status_code == 503

    def test_a_viewer_cannot_reconcile(self, client, app, viewer_headers, tmp_path):
        _wire(app, tmp_path)
        resp = client.post("/api/v1/sources/reconcile-apps", headers=viewer_headers)
        assert resp.status_code == 403
