#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_system_deployment.py
#  Purpose:      GET /api/v1/system/deployment - what the console must not guess
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""One endpoint for what this deployment IS, so no console pane has to assume.

Without it a console has to offer the widest deployment: every optional app
listed, both transports selectable, an Edge VPN card on a tier that runs no
mesh. Everything here is already read by the engine -- the transport pair from
settings, the profile the deployer injected, the versions /system/version
serves -- so the point of the endpoint is that ONE function answers both routes
and the two can never disagree.
"""

from __future__ import annotations

import threading

from dfe_engine import __version__
from dfe_engine.api.v1 import system
from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo

PINS = 'base:\n  dfe-infra: "2.2.0-rc.13"\n  dfe-schemas: "2.2.0"\nchannel: "rc"\n'


def _wire(app, tmp_path, pins: str | None = PINS) -> GitCrud:
    """Give the app a deploy repo, optionally carrying a pins.yaml."""
    repo_path = tmp_path / "deploy"
    gc = GitCrud(GitopsRepo(local_path=str(repo_path), push=False), default_registry())
    if pins is not None:
        (repo_path / "pins.yaml").write_text(pins)
    app.state.gitcrud = gc
    return gc


class TestDeploymentFacts:
    def test_reports_the_profile_the_deployer_injected(self, client, app, admin_headers):
        app.state.settings.deployment.profile = "mesh"

        body = client.get("/api/v1/system/deployment", headers=admin_headers).json()

        assert body["profile"] == "mesh"

    def test_an_unset_profile_is_empty_rather_than_guessed(self, client, admin_headers):
        resp = client.get("/api/v1/system/deployment", headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["profile"] == ""

    def test_a_brokerless_deployment_offers_direct_only(self, client, app, admin_headers):
        app.state.settings.transport.bus_present = False
        app.state.settings.transport.default = "direct"

        body = client.get("/api/v1/system/deployment", headers=admin_headers).json()

        assert body["transports"] == {"default": "direct", "available": ["direct"]}

    def test_a_deployment_with_a_bus_offers_the_bus_alone(self, client, app, admin_headers):
        # The loader consumes the topic here and serves no push listener, so a
        # console offering direct would offer a flow that lands nowhere.
        app.state.settings.transport.bus_present = True
        app.state.settings.transport.default = "bus"

        body = client.get("/api/v1/system/deployment", headers=admin_headers).json()

        assert body["transports"] == {"default": "bus", "available": ["bus"]}

    def test_the_mesh_pair_is_reported_together(self, client, app, admin_headers):
        app.state.settings.transport.mesh_enabled = True
        app.state.settings.transport.mesh_namespace = "dfe-mesh"

        body = client.get("/api/v1/system/deployment", headers=admin_headers).json()

        assert body["mesh"] == {"enabled": True, "namespace": "dfe-mesh"}

    def test_no_mesh_is_reported_as_off_with_no_namespace(self, client, admin_headers):
        body = client.get("/api/v1/system/deployment", headers=admin_headers).json()

        assert body["mesh"] == {"enabled": False, "namespace": ""}

    def test_a_compose_deployment_that_renders_no_app_config_applies_no_routing(
        self, client, app, admin_headers
    ):
        # Nothing carries the overlay into the container, so the write the engine
        # makes on a deploy stops at the deploy repo.
        app.state.settings.deployment.target = "docker"

        body = client.get("/api/v1/system/deployment", headers=admin_headers).json()

        assert body["applies_routing"] is False

    def test_a_compose_deployment_that_renders_its_app_config_applies_it(
        self, client, app, admin_headers
    ):
        app.state.settings.deployment.target = "docker"
        app.state.settings.deployment.app_config_dir = "/app/app-config"

        body = client.get("/api/v1/system/deployment", headers=admin_headers).json()

        assert body["applies_routing"] is True

    def test_kubernetes_applies_it(self, client, app, admin_headers):
        app.state.settings.deployment.target = "kubernetes"

        body = client.get("/api/v1/system/deployment", headers=admin_headers).json()

        assert body["applies_routing"] is True

    def test_a_target_the_deployer_did_not_name_is_not_taken_for_compose(
        self, client, app, admin_headers
    ):
        # Only the one target that consumes no overlay reports False; reporting it
        # for an unnamed target would tell a console the routing is dead wherever
        # the deployer left the value out.
        assert app.state.settings.deployment.target == "unknown"

        body = client.get("/api/v1/system/deployment", headers=admin_headers).json()

        assert body["applies_routing"] is True

    def test_the_versions_are_the_ones_version_already_serves(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)

        facts = client.get("/api/v1/system/deployment", headers=admin_headers).json()
        version = client.get("/api/v1/system/version", headers=admin_headers).json()

        assert facts["stack"] == version["stack"] == "2.2.0-rc.13"
        assert facts["engine"] == version["engine"] == __version__
        assert facts["ui"] == version["ui"]
        assert facts["source"] == version["source"] == "deploy-repo"

    def test_the_chart_pin_answers_for_ui_when_the_deploy_repo_pins_none(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path)
        app.state.settings.ui_version = "v1.5.1"

        body = client.get("/api/v1/system/deployment", headers=admin_headers).json()

        assert body["ui"] == "v1.5.1"

    def test_the_deploy_repo_pin_wins_over_the_chart(self, client, app, admin_headers, tmp_path):
        # The pins are what the operator chose; the chart value is only what
        # happened to render this pod.
        _wire(app, tmp_path, PINS + 'overrides:\n  apps:\n    dfe-ui: "v1.20.0@sha256:abc"\n')
        app.state.settings.ui_version = "v1.5.1"

        body = client.get("/api/v1/system/deployment", headers=admin_headers).json()

        assert body["ui"] == "v1.20.0@sha256:abc"

    def test_ui_is_null_when_neither_states_one(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)

        assert client.get("/api/v1/system/deployment", headers=admin_headers).json()["ui"] is None

    def test_apps_carries_every_pinned_component(self, client, app, admin_headers, tmp_path):
        _wire(
            app,
            tmp_path,
            PINS
            + 'overrides:\n  apps:\n    dfe-ui: "v1.20.0@sha256:abc"\n'
            + '    dfe-receiver: "v2.3.0@sha256:def"\n',
        )

        body = client.get("/api/v1/system/deployment", headers=admin_headers).json()

        assert body["apps"] == {
            "dfe-ui": "v1.20.0@sha256:abc",
            "dfe-receiver": "v2.3.0@sha256:def",
        }
        assert body["ui"] == body["apps"]["dfe-ui"]

    def test_apps_is_empty_without_pins(self, client, admin_headers):
        body = client.get("/api/v1/system/deployment", headers=admin_headers).json()

        assert body["apps"] == {}

    def test_the_pins_are_read_under_the_publish_lock(
        self, client, app, admin_headers, tmp_path, monkeypatch
    ):
        """pins.yaml comes off the working tree, so a publish must not reset under it."""
        gc = _wire(app, tmp_path)
        contended: list[bool] = []
        real_load_pins = system.load_pins

        def probe() -> None:
            # RLock is reentrant for its owner, so the probe runs on another thread.
            taken = gc.repo._lock.acquire(blocking=False)
            contended.append(not taken)
            if taken:
                gc.repo._lock.release()

        def probing_load_pins(path):
            prober = threading.Thread(target=probe)
            prober.start()
            prober.join(timeout=10.0)
            return real_load_pins(path)

        monkeypatch.setattr(system, "load_pins", probing_load_pins)

        body = client.get("/api/v1/system/deployment", headers=admin_headers).json()

        assert body["stack"] == "2.2.0-rc.13"
        assert contended == [True]

    def test_any_authenticated_user_can_read_it(self, client, viewer_headers):
        resp = client.get("/api/v1/system/deployment", headers=viewer_headers)

        assert resp.status_code == 200, resp.text

    def test_anonymous_is_refused(self, client):
        assert client.get("/api/v1/system/deployment").status_code == 401
