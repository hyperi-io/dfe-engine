#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_config_review_routing.py
#  Purpose:      Field map, alert, service and meta-schema writes through the deploy repo
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""With gitops on, these four routes write the deploy repo and report where it landed.

A production+team write sits on a review branch, so a 201 or 204 with no signal
tells the operator a change is live that no replica reads yet. The header contract
is the one the hunt and rule routes already use.
"""

import pytest

from dfe_engine.api.deps import _registries
from dfe_engine.appmgmt import contract
from dfe_engine.fieldmap.registry import FIELDMAP_CLASS, FieldMapRegistry
from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitcrud.forge import PullRequest
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.governance import PolicyStore
from dfe_engine.hunts.alert import ALERT_CLASS, AlertDestinationRegistry
from dfe_engine.hunts.deploy_repo import DeployRepoStore
from dfe_engine.schema.registry import META_CLASS, SchemaRegistry
from dfe_engine.services.registry import SERVICE_CONFIG_CLASS, ServiceConfigRegistry
from dfe_engine.yaml_utils import yaml_dump


class _RecordingForge:
    """ForgeProvider seam double: records the PR the write opens."""

    def __init__(self, url: str) -> None:
        self.calls: list[dict] = []
        self._url = url

    def open_pull_request(self, *, head, base, title, body) -> PullRequest:
        self.calls.append({"head": head, "base": base, "title": title, "body": body})
        return PullRequest(number=7, url=self._url, branch=head)


@pytest.fixture
def deploy_repo(client, tmp_path) -> GitCrud:
    """A real local deploy repo, after ``client`` so the lifespan's registries exist to replace."""
    crud = GitCrud(GitopsRepo(local_path=str(tmp_path / "deploy"), push=False), default_registry())
    # An empty repo has no base to branch a review from, so main carries a first commit.
    crud.put("gov_settings", "seed", {"seed": True}, "seed")
    return crud


def _rebind(crud: GitCrud, tmp_path, *, environment: str, mode: str, forge=None) -> None:
    """Point the four registries at the deploy repo, at one posture."""

    def store(cls_name: str, commit_type: str) -> DeployRepoStore:
        return DeployRepoStore(
            crud,
            cls_name,
            environment=environment,
            mode=mode,
            forge=forge,
            commit_type=commit_type,
        )

    replacements = {
        "fieldmap": FieldMapRegistry(deploy_repo=store(FIELDMAP_CLASS, "schema")),
        "alert_destinations": AlertDestinationRegistry(deploy_repo=store(ALERT_CLASS, "hunt")),
        "service_config": ServiceConfigRegistry(deploy_repo=store(SERVICE_CONFIG_CLASS, "cfg")),
        "meta_schema": SchemaRegistry(
            tmp_path / "shipped-schemas", deploy_repo=store(META_CLASS, "schema")
        ),
    }
    for key, registry in replacements.items():
        previous = _registries.get(key)
        if previous is not None:
            previous.close()
        _registries[key] = registry


def _on_main(crud: GitCrud, rel: str) -> bool:
    return (crud.repo_path / rel).is_file()


_META = {
    "current": "1.0.0",
    "versions": {
        "1.0.0": {
            "date": "2026-10-22",
            "type": "model",
            "summary": "first",
            "columns": [{"name": "rule", "type": "string", "_field_type": "base"}],
        }
    },
}


class TestDevPostureLandsOnMain:
    def test_a_field_map_create_carries_no_review_header(
        self, client, admin_headers, deploy_repo, tmp_path
    ):
        _rebind(deploy_repo, tmp_path, environment="dev", mode="team")

        resp = client.post(
            "/api/v1/field-maps",
            json={"standard": "ecs", "mappings": {"user.name": "user_name"}},
            headers=admin_headers,
        )

        assert resp.status_code == 201, resp.text
        assert "X-DFE-Review-Required" not in resp.headers
        assert _on_main(deploy_repo, "config/fieldmaps/ecs/_default.yaml")

    def test_a_service_config_save_is_read_back_from_the_deploy_repo(
        self, client, admin_headers, deploy_repo, tmp_path
    ):
        _rebind(deploy_repo, tmp_path, environment="dev", mode="team")

        saved = client.put(
            "/api/v1/services/new-service/prod", json={"alpha": 1}, headers=admin_headers
        )
        got = client.get("/api/v1/services/new-service/prod", headers=admin_headers)

        assert saved.status_code == 200, saved.text
        assert got.json()["config"] == {"alpha": 1}
        assert _on_main(deploy_repo, "config/services/new-service-prod.yaml")


class TestProductionTeamWritesReportReview:
    def test_a_field_map_create(self, client, admin_headers, deploy_repo, tmp_path):
        forge = _RecordingForge("http://forge/pr/71")
        _rebind(deploy_repo, tmp_path, environment="production", mode="team", forge=forge)

        resp = client.post(
            "/api/v1/field-maps",
            json={"standard": "ecs", "mappings": {"user.name": "user_name"}},
            headers=admin_headers,
        )

        assert resp.status_code == 201, resp.text
        assert resp.headers["X-DFE-Review-Required"] == "true"
        assert resp.headers["X-DFE-PR-Url"] == "http://forge/pr/71"
        assert not _on_main(deploy_repo, "config/fieldmaps/ecs/_default.yaml")

    def test_a_field_map_delete(self, client, admin_headers, deploy_repo, tmp_path):
        _rebind(deploy_repo, tmp_path, environment="dev", mode="solo")
        client.post(
            "/api/v1/field-maps",
            json={"standard": "ecs", "source": "win", "mappings": {"a": "b"}},
            headers=admin_headers,
        )
        _rebind(
            deploy_repo,
            tmp_path,
            environment="production",
            mode="team",
            forge=_RecordingForge("http://forge/pr/72"),
        )

        resp = client.delete("/api/v1/field-maps/ecs/win", headers=admin_headers)

        assert resp.status_code == 204
        assert resp.headers["X-DFE-Review-Required"] == "true"
        assert _on_main(deploy_repo, "config/fieldmaps/ecs/win.yaml")

    def test_an_alert_destination_create_answers_with_the_url_masked(
        self, client, admin_headers, deploy_repo, tmp_path
    ):
        _rebind(
            deploy_repo,
            tmp_path,
            environment="production",
            mode="team",
            forge=_RecordingForge("http://forge/pr/73"),
        )

        resp = client.post(
            "/api/v1/alerts/destinations",
            json={"name": "oncall", "url": "slack://T1/B2/C3/#alerts"},
            headers=admin_headers,
        )

        assert resp.status_code == 201, resp.text
        assert resp.headers["X-DFE-Review-Required"] == "true"
        assert resp.json()["url"] == contract.REDACTED
        assert resp.json()["url_scheme"] == "slack"
        assert not _on_main(deploy_repo, "config/alert-destinations/oncall.yaml")

    def test_an_alert_destination_delete(self, client, admin_headers, deploy_repo, tmp_path):
        _rebind(deploy_repo, tmp_path, environment="dev", mode="solo")
        client.post(
            "/api/v1/alerts/destinations",
            json={"name": "oncall", "url": "slack://T1/B2/C3"},
            headers=admin_headers,
        )
        _rebind(
            deploy_repo,
            tmp_path,
            environment="production",
            mode="team",
            forge=_RecordingForge("http://forge/pr/74"),
        )

        resp = client.delete("/api/v1/alerts/destinations/oncall", headers=admin_headers)

        assert resp.status_code == 204
        assert resp.headers["X-DFE-Review-Required"] == "true"
        assert _on_main(deploy_repo, "config/alert-destinations/oncall.yaml")

    def test_a_service_config_save_and_delete(self, client, admin_headers, deploy_repo, tmp_path):
        _rebind(deploy_repo, tmp_path, environment="dev", mode="solo")
        client.put("/api/v1/services/new-service/prod", json={"alpha": 1}, headers=admin_headers)
        _rebind(
            deploy_repo,
            tmp_path,
            environment="production",
            mode="team",
            forge=_RecordingForge("http://forge/pr/75"),
        )

        saved = client.put(
            "/api/v1/services/new-service/prod", json={"alpha": 2}, headers=admin_headers
        )
        deleted = client.delete("/api/v1/services/new-service/prod", headers=admin_headers)

        assert saved.headers["X-DFE-Review-Required"] == "true"
        assert deleted.status_code == 204
        assert deleted.headers["X-DFE-Review-Required"] == "true"
        assert deploy_repo.get(SERVICE_CONFIG_CLASS, "new-service-prod") == {"alpha": 1}

    def test_a_meta_schema_create_and_new_version(
        self, client, admin_headers, deploy_repo, tmp_path
    ):
        _rebind(deploy_repo, tmp_path, environment="dev", mode="solo")
        created = client.post(
            "/api/v1/schemas/definitions/meta/acme/firewall", json=_META, headers=admin_headers
        )
        assert created.status_code == 201, created.text
        _rebind(
            deploy_repo,
            tmp_path,
            environment="production",
            mode="team",
            forge=_RecordingForge("http://forge/pr/76"),
        )

        resp = client.post(
            "/api/v1/schemas/definitions/meta/acme/firewall/versions",
            json={
                "type": "addition",
                "summary": "add action",
                "columns": [
                    {"name": "rule", "type": "string", "_field_type": "base"},
                    {"name": "action", "type": "string", "_field_type": "base"},
                ],
            },
            headers=admin_headers,
        )

        assert resp.status_code == 201, resp.text
        assert resp.headers["X-DFE-Review-Required"] == "true"
        assert resp.json()["current"] == "1.1.0"
        assert deploy_repo.get(META_CLASS, "meta/acme/firewall")["current"] == "1.0.0"


class TestTheDeployRepoLayer:
    def test_a_schema_only_the_image_ships_cannot_be_deleted(
        self, client, admin_headers, deploy_repo, tmp_path
    ):
        """Every replica ships its own copy, and no deploy-repo commit can remove those."""
        shipped = tmp_path / "shipped-schemas" / "meta" / "acme" / "proxy.yaml"
        shipped.parent.mkdir(parents=True)
        yaml_dump(_META, shipped)
        _rebind(deploy_repo, tmp_path, environment="dev", mode="solo")

        resp = client.delete("/api/v1/schemas/definitions/meta/acme/proxy", headers=admin_headers)

        assert resp.status_code == 409, resp.text
        assert shipped.is_file()

    def test_a_destination_name_no_deploy_repo_file_can_hold_is_a_422(
        self, client, admin_headers, deploy_repo, tmp_path
    ):
        _rebind(deploy_repo, tmp_path, environment="dev", mode="solo")

        resp = client.post(
            "/api/v1/alerts/destinations",
            json={"name": "on call", "url": "slack://T1/B2/C3"},
            headers=admin_headers,
        )

        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "validation_error"


class TestTheAppriseUrlStaysACredential:
    """The name rule reads `url` as a setting, so the class says otherwise itself."""

    def test_the_generic_vars_route_masks_it(
        self, app, client, admin_headers, viewer_headers, deploy_repo, tmp_path
    ):
        _rebind(deploy_repo, tmp_path, environment="dev", mode="solo")
        app.state.gitcrud = deploy_repo
        client.post(
            "/api/v1/alerts/destinations",
            json={"name": "oncall", "url": "slack://T1/B2/C3/#alerts"},
            headers=admin_headers,
        )

        resp = client.get(
            "/api/v1/gitops/classes/alert_destinations/resources/oncall/vars",
            headers=viewer_headers,
        )

        assert resp.status_code == 200, resp.text
        assert {row["path"]: row["value"] for row in resp.json()}["url"] == contract.REDACTED
        assert "T1/B2/C3" not in resp.text

    def test_an_action_may_not_carry_one(self, app, client, admin_headers, deploy_repo, tmp_path):
        app.state.settings.env = "dev"
        app.state.gitcrud = deploy_repo
        app.state.policy_store = PolicyStore(deploy_repo)
        action = {
            "name": "repoint-oncall",
            "changes": [
                {
                    "cls": "alert_destinations",
                    "name": "oncall",
                    "path": "url",
                    "value": "slack://T9/B9/C9",
                }
            ],
        }

        resp = client.post("/api/v1/governance/admin/actions", json=action, headers=admin_headers)

        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "credential_in_action"
