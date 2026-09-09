#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_system_version.py
#  Purpose:      GET /api/v1/system/version - what the console footer shows
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The stack version comes from the deploy repo's pins.yaml, through gitcrud.

A deploy with no pins base still knows what stood it up, because the chart passes
that in, so the footer falls back to it. With neither, the engine's own version is
the whole answer, which is what the docker and dev profiles run. Every page of the
console calls this, so it is open to any authenticated user and closed to an
anonymous one.
"""

from __future__ import annotations

from dfe_engine import __version__
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


class TestGetVersion:
    def test_stack_comes_from_the_deploy_repo(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path)
        resp = client.get("/api/v1/system/version", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["stack"] == "2.2.0-rc.13"
        assert body["source"] == "deploy-repo"
        assert body["engine"] == __version__
        assert body["ui"] is None

    def test_ui_comes_from_the_deploy_repo_pin(self, client, app, admin_headers, tmp_path):
        _wire(app, tmp_path, PINS + 'overrides:\n  apps:\n    dfe-ui: "v1.20.0@sha256:abc"\n')
        body = client.get("/api/v1/system/version", headers=admin_headers).json()
        assert body["ui"] == "v1.20.0@sha256:abc"

    def test_engine_only_without_a_deploy_repo(self, client, admin_headers):
        resp = client.get("/api/v1/system/version", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["stack"] is None
        assert body["ui"] is None
        assert body["source"] == "engine"
        assert body["engine"] == __version__

    def test_engine_only_when_the_deploy_repo_has_no_pins(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path, pins=None)
        body = client.get("/api/v1/system/version", headers=admin_headers).json()
        assert body["stack"] is None
        assert body["source"] == "engine"

    def test_the_deployment_answers_when_the_deploy_repo_pins_nothing(
        self, client, app, admin_headers, tmp_path
    ):
        _wire(app, tmp_path, pins=None)
        app.state.settings.stack_version = "2.2.0-rc.13"

        body = client.get("/api/v1/system/version", headers=admin_headers).json()

        assert body["stack"] == "2.2.0-rc.13"
        assert body["source"] == "deployment"

    def test_the_pins_win_over_what_deployed_the_pod(self, client, app, admin_headers, tmp_path):
        # The pins are what the operator chose; the deployment value is only
        # what happened to stand this pod up.
        _wire(app, tmp_path)
        app.state.settings.stack_version = "2.2.0-rc.12"

        body = client.get("/api/v1/system/version", headers=admin_headers).json()

        assert body["stack"] == "2.2.0-rc.13"
        assert body["source"] == "deploy-repo"

    def test_any_authenticated_user_can_read_it(self, client, app, viewer_headers, tmp_path):
        _wire(app, tmp_path)
        resp = client.get("/api/v1/system/version", headers=viewer_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["stack"] == "2.2.0-rc.13"

    def test_anonymous_is_refused(self, client, app, tmp_path):
        _wire(app, tmp_path)
        assert client.get("/api/v1/system/version").status_code == 401
