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

import pytest

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
        "schema": {"meta_schema": "meta/syslog"},
        "fetcher": {
            "source_type": "crates_io",
            "config": {"crates": ["dfe-fetcher"]},
            **fetcher,
        },
    }


def _rules(gc) -> list[str]:
    """The ``_source`` each compiled receiver row sets, in row order."""
    doc = gc.get("helmvars", RECEIVER_OVERLAY)
    rows = doc.get("config", {}).get("rules", {}).get("rules", [])
    return [row["set"]["_source"]["value"] for row in rows]


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

        resp = client.delete("/api/v1/sources/kvproof", headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["apps_synced"] == ["dfe-receiver/main: sync routing"]
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


def _refuse_overlay_writes(app) -> None:
    """Production with team review and no forge: every overlay write is refused, source writes are not."""
    app.state.settings.env = "production"
    app.state.settings.gitops.mode = "team"
    app.state.forge = None


class TestACreateTheAppsDidNotFollowIsAccepted:
    """A stored source whose reconcile failed answers 202, so the status alone tells the two apart."""

    def test_the_create_answers_202_and_says_why(self, client, app, admin_headers, tmp_path):
        gc = _wire(app, tmp_path)
        _refuse_overlay_writes(app)

        resp = client.post("/api/v1/sources", json=_receiver_body("kvproof"), headers=admin_headers)

        assert resp.status_code == 202, resp.text
        assert "review PR is required" in resp.json()["apps_sync_error"]
        assert resp.json()["source"] == "kvproof"
        assert client.get("/api/v1/sources/kvproof", headers=admin_headers).status_code == 200
        assert _rules(gc) == []

    def test_the_retry_then_brings_the_apps_into_step(self, client, app, admin_headers, tmp_path):
        gc = _wire(app, tmp_path)
        _refuse_overlay_writes(app)
        client.post("/api/v1/sources", json=_receiver_body("kvproof"), headers=admin_headers)
        app.state.settings.env = "dev"

        resp = client.post("/api/v1/sources/reconcile-apps", headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert _rules(gc) == ["kvproof"]

    def test_a_catalogue_create_answers_the_same(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        _refuse_overlay_writes(app)

        resp = client.post(
            "/api/v1/sources/from-catalogue",
            json={"entries": [{"entry": "okta", "intake": "beats"}]},
            headers=admin_headers,
        )

        assert resp.status_code == 202, resp.text
        assert resp.json()["apps_sync_error"]


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
        # The deploy endpoint needs ClickHouse. Marking the version deployed is
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
        doc["config"]["rules"]["rules"] = []
        gc.put("helmvars", RECEIVER_OVERLAY, doc, "test", message="test: hand edit")

        resp = client.post("/api/v1/sources/reconcile-apps", headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "changes": ["dfe-receiver/main: sync routing"],
            "restart_required": [],
            "topics_removed": [],
            "topics_pending_removal": [],
        }
        assert _rules(gc) == ["kvproof"]

    def test_reconcile_apps_without_a_deploy_repo_is_503(self, client, admin_headers):
        resp = client.post("/api/v1/sources/reconcile-apps", headers=admin_headers)
        assert resp.status_code == 503

    def test_a_viewer_cannot_reconcile(self, client, app, viewer_headers, tmp_path):
        _wire(app, tmp_path)
        resp = client.post("/api/v1/sources/reconcile-apps", headers=viewer_headers)
        assert resp.status_code == 403


class TestOneFetcherRunsEveryFetchedSourceOnCompose:
    """A published Compose install runs one fetcher, so a source it cannot take is refused at save."""

    def _published(self, app, tmp_path, monkeypatch, *, hosted: str = "") -> None:
        """Move the deployment onto a published install: Compose, rendered config, no index."""
        from dfe_engine.settings import get_settings

        # The route reads the app's settings and the save gate the process's.
        found = (app.state.settings.deployment, get_settings().deployment)
        for deployment in {id(d): d for d in found}.values():
            monkeypatch.setattr(deployment, "target", "docker")
            monkeypatch.setattr(deployment, "app_config_dir", str(tmp_path / "app-config"))
            monkeypatch.setattr(deployment, "app_env_dir", "")
            monkeypatch.setattr(deployment, "hosted_instances", hosted)

    def _running(self, client, admin_headers, name: str, **fetcher) -> None:
        from dfe_engine.api.deps import _registries

        body = _fetcher_body(name, **{k: v for k, v in fetcher.items() if v is not None})
        created = client.post("/api/v1/sources", json=body, headers=admin_headers)
        assert created.status_code == 201, created.text
        _registries["source"].set_deployed_version(name, "1.0.0")
        assert (
            client.post("/api/v1/sources/reconcile-apps", headers=admin_headers).status_code == 200
        )

    def _polled(self, tmp_path) -> dict:
        from dfe_engine.yaml_utils import yaml_load

        rendered = yaml_load(tmp_path / "app-config" / FETCHER / "fetcher.yaml")
        return rendered["sources"]

    def test_a_clashing_source_is_refused_and_the_running_one_keeps_running(
        self, client, app, admin_headers, tmp_path, monkeypatch
    ):
        self._save_gate(app, tmp_path)
        self._published(app, tmp_path, monkeypatch)
        self._running(client, admin_headers, "crates-b")

        # crates_io is one flat stanza per process, and this name sorts first.
        refused = client.post(
            "/api/v1/sources", json=_fetcher_body("crates-a"), headers=admin_headers
        )

        assert refused.status_code in (400, 422), refused.text
        assert "cannot share the one dfe-fetcher this deployment runs" in refused.text
        assert "config.sources.crates_io" in refused.text
        assert self._polled(tmp_path)["crates_io"]["topic"] == "crates-b"

    def _save_gate(self, app, tmp_path):
        """A deploy repo, and a registry that runs the source save gate with the engine's orgs."""
        from dfe_engine.api.deps import _registries
        from dfe_engine.source.registry import SourceRegistry

        gc = _wire(app, tmp_path)
        _registries["source"] = SourceRegistry(crud=gc)
        return gc

    @pytest.mark.parametrize(
        ("running_org", "newcomer_org", "reason"),
        [
            # One org on both: the second crates_io stanza is the clash.
            ("acme", "acme", "its config.sources.crates_io.topic is already set"),
            # One process stamps one org on everything it emits.
            (None, "acme", "its config.org_id differs from crates-b's"),
        ],
    )
    def test_a_source_naming_an_org_is_judged_with_that_org(
        self, client, app, admin_headers, tmp_path, monkeypatch, running_org, newcomer_org, reason
    ):
        self._save_gate(app, tmp_path)
        app.state.org_registry.create("acme", display_name="Acme")
        self._published(app, tmp_path, monkeypatch)
        self._running(client, admin_headers, "crates-b", org=running_org)

        refused = client.post(
            "/api/v1/sources",
            json=_fetcher_body("crates-a", org=newcomer_org),
            headers=admin_headers,
        )

        assert refused.status_code in (400, 422), refused.text
        assert reason in refused.text
        assert self._polled(tmp_path)["crates_io"]["topic"] == "crates-b"

    def test_with_no_orgs_to_read_a_source_is_judged_by_its_org_name(
        self, client, app, admin_headers, tmp_path, monkeypatch
    ):
        self._save_gate(app, tmp_path)
        app.state.org_registry.create("acme", display_name="Acme")
        self._published(app, tmp_path, monkeypatch)
        self._running(client, admin_headers, "crates-b")
        monkeypatch.setattr("dfe_engine.source.registry._tenancy_reader", None)

        refused = client.post(
            "/api/v1/sources", json=_fetcher_body("crates-a", org="acme"), headers=admin_headers
        )

        assert refused.status_code in (400, 422), refused.text
        assert "its config.org_id differs from crates-b's" in refused.text
        assert "holds no orgs" not in refused.text

    @pytest.mark.parametrize("edited", ["crates-b", "pypi-a"])
    def test_an_update_the_one_fetcher_cannot_take_is_refused_and_both_keep_running(
        self, client, app, admin_headers, tmp_path, monkeypatch, edited
    ):
        self._save_gate(app, tmp_path)
        self._published(app, tmp_path, monkeypatch)
        self._running(client, admin_headers, "crates-b")
        self._running(
            client,
            admin_headers,
            "pypi-a",
            source_type="pypi",
            config={"packages": ["scalo"]},
        )

        resp = client.put(
            f"/api/v1/apps/{FETCHER}/{edited}/config",
            json={"changes": {"extraEnv.ONLY_ME": "1"}},
            headers=admin_headers,
        )

        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "shared_instance_conflict"
        assert "extraEnv.ONLY_ME" in resp.json()["detail"]
        polled = self._polled(tmp_path)
        assert polled["crates_io"]["topic"] == "crates-b"
        assert polled["pypi"]["topic"] == "pypi-a"

    def test_a_generic_helm_write_that_clashes_is_refused(
        self, client, app, admin_headers, tmp_path, monkeypatch
    ):
        gc = self._save_gate(app, tmp_path)
        self._published(app, tmp_path, monkeypatch)
        self._running(client, admin_headers, "crates-b")

        resp = client.put(
            f"/api/v1/helm/files/{FETCHER}-crates-a-values/vars/config.sources.crates_io.topic",
            json={"value": "crates-a"},
            headers=admin_headers,
        )

        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "shared_instance_conflict"
        assert f"{FETCHER}-crates-a-values" not in gc.list("helmvars")
        assert self._polled(tmp_path)["crates_io"]["topic"] == "crates-b"

    def test_a_hand_made_instance_outside_the_hosted_set_is_refused(
        self, client, app, admin_headers, tmp_path, monkeypatch
    ):
        from dfe_engine.api.deps import _registries

        _wire(app, tmp_path)
        # Saved before the move, so the route's own check is what answers.
        for name in ("edge", "other"):
            body = {**_receiver_body(name), "transform": {"engine": "vrl"}}
            assert (
                client.post("/api/v1/sources", json=body, headers=admin_headers).status_code == 201
            )
            _registries["source"].set_deployed_version(name, "1.0.0")
        self._published(app, tmp_path, monkeypatch, hosted="dfe-transform-vrl=edge")

        other = client.post(
            "/api/v1/apps/dfe-transform-vrl/instances",
            json={"instance": "other", "values": {}},
            headers=admin_headers,
        )

        assert other.status_code == 409, other.text
        assert other.json()["code"] == "not_hosted"
        assert "only for the sources it was published with (edge)" in other.json()["detail"]


class TestTheReconcileReportsTheRestartsItsOwnWritesNeed:
    """Where the engine renders the app config, the write that rendered it reports.

    The regression these guard: a Compose stack deployed a transformed source,
    the engine wrote the instance overlay and rendered the transform's config
    from it, and the deploy answered nothing - so the operator ran nothing and no
    record ever moved. A transform is per-config, so the source's instance has no
    container until an ``up`` creates one from the index this render wrote.
    """

    TRANSFORM = "dfe-transform-elastic"
    RECREATE = f"recreate required: make apply SERVICES={TRANSFORM}-elreg"

    def _compose(self, app, tmp_path):
        """A deploy repo, the app-config directory a Compose stack mounts, and the env directory its make reads the index from."""
        gc = _wire(app, tmp_path)
        app.state.settings.deployment.target = "docker"
        app.state.settings.deployment.app_config_dir = str(tmp_path / "app-config")
        app.state.settings.deployment.app_env_dir = str(tmp_path / "app-env")
        return gc

    def _deployed_transform_source(self, client, admin_headers, name: str) -> None:
        from dfe_engine.api.deps import _registries

        body = {
            "source": name,
            "match": {"field": "_source", "value": name},
            "transform": {"engine": "elastic"},
        }
        assert client.post("/api/v1/sources", json=body, headers=admin_headers).status_code == 201
        _registries["source"].set_deployed_version(name, "1.0.0")

    def test_a_new_transform_instance_names_the_container_to_bring_up(
        self, client, app, admin_headers, tmp_path
    ):
        from dfe_engine.appmgmt import appconfig

        gc = self._compose(app, tmp_path)
        # The render every deployment does at startup, before any source exists.
        appconfig.render_and_report(gc, app.state.settings)
        self._deployed_transform_source(client, admin_headers, "elreg")

        resp = client.post("/api/v1/sources/reconcile-apps", headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["changes"] == [f"{self.TRANSFORM}/elreg: deploy instance"]
        assert resp.json()["restart_required"] == [self.RECREATE]

    def test_a_new_variant_on_a_running_instance_names_the_restart(
        self, client, app, admin_headers, tmp_path
    ):
        # The variant is the app's own config.source.name, which the elastic
        # transform reads once at startup. The instance is already running here.
        from dfe_engine.appmgmt import appconfig

        gc = self._compose(app, tmp_path)
        appconfig.render_and_report(gc, app.state.settings)
        self._deployed_transform_source(client, admin_headers, "elreg")
        client.post("/api/v1/sources/reconcile-apps", headers=admin_headers)

        resp = client.put(
            "/api/v1/sources/elreg",
            json={
                "source": "elreg",
                "match": {"field": "_source", "value": "elreg"},
                "transform": {"engine": "elastic", "variant": "filebeat.cisco_ios.default"},
            },
            headers=admin_headers,
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["restart_required"] == [
            f"restart required: make apply SERVICES={self.TRANSFORM}-elreg"
        ]
        doc = gc.get("helmvars", f"{self.TRANSFORM}-elreg-values")
        assert doc["config"]["source"]["name"] == "filebeat.cisco_ios.default"

    def test_a_delete_names_the_command_that_takes_the_container_away(
        self, client, app, admin_headers, tmp_path
    ):
        from dfe_engine.appmgmt import appconfig

        gc = self._compose(app, tmp_path)
        appconfig.render_and_report(gc, app.state.settings)
        self._deployed_transform_source(client, admin_headers, "elreg")
        client.post("/api/v1/sources/reconcile-apps", headers=admin_headers)
        rendered = tmp_path / "app-config" / self.TRANSFORM / "elreg"
        assert rendered.is_dir()

        resp = client.delete("/api/v1/sources/elreg", headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert f"{self.TRANSFORM}/elreg: undeploy instance" in resp.json()["apps_synced"]
        assert resp.json()["restart_required"].count(appconfig.REMOVE_HINT) == 1
        assert not rendered.exists()
        again = client.post("/api/v1/sources/reconcile-apps", headers=admin_headers)
        assert again.json()["restart_required"] == []

    def test_a_bulk_delete_names_the_same_command(self, client, app, admin_headers, tmp_path):
        from dfe_engine.appmgmt import appconfig

        gc = self._compose(app, tmp_path)
        appconfig.render_and_report(gc, app.state.settings)
        self._deployed_transform_source(client, admin_headers, "elreg")
        client.post("/api/v1/sources/reconcile-apps", headers=admin_headers)

        resp = client.post(
            "/api/v1/sources/bulk",
            json={"action": "delete", "sources": ["elreg"]},
            headers=admin_headers,
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["succeeded"] == ["elreg"]
        assert f"{self.TRANSFORM}/elreg: undeploy instance" in resp.json()["apps_synced"]
        assert resp.json()["restart_required"].count(appconfig.REMOVE_HINT) == 1
        assert resp.json()["apps_sync_error"] is None

    def test_a_bulk_state_change_reports_its_reconcile(self, client, app, admin_headers, tmp_path):
        self._compose(app, tmp_path)
        self._deployed_transform_source(client, admin_headers, "elreg")
        client.post("/api/v1/sources/reconcile-apps", headers=admin_headers)

        resp = client.post(
            "/api/v1/sources/bulk",
            json={"action": "dormant", "sources": ["elreg"]},
            headers=admin_headers,
        )

        assert resp.status_code == 200, resp.text
        assert f"{self.TRANSFORM}/elreg: undeploy instance" in resp.json()["apps_synced"]

    def test_the_deploying_write_reports_it_only_once(self, client, app, admin_headers, tmp_path):
        from dfe_engine.appmgmt import appconfig

        gc = self._compose(app, tmp_path)
        appconfig.render_and_report(gc, app.state.settings)
        self._deployed_transform_source(client, admin_headers, "elreg")
        client.post("/api/v1/sources/reconcile-apps", headers=admin_headers)

        resp = client.post("/api/v1/sources/reconcile-apps", headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "changes": [],
            "restart_required": [],
            "topics_removed": [],
            "topics_pending_removal": [],
        }
