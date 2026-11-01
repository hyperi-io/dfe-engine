#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_sources_review_routing.py
#  Purpose:      Source and derived-schema writes routed to review say so, and change nothing else
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A production+team source or derived-schema write answers "awaiting review", not "saved".

The source routes answer with a status body, so it carries ``review_required`` and
``pr_url`` as an app-overlay save's ``WriteResult`` does. The derived-schema routes
answer with the resource or a 204, so they use the review headers the hunt routes
use. A delete held for review leaves the source's topics and records alone.
"""

from __future__ import annotations

import pytest

from dfe_engine.api.deps import _registries
from dfe_engine.api.v1 import sources as sources_module
from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitcrud.forge import PullRequest
from dfe_engine.gitcrud.routing import WritePosture
from dfe_engine.gitops.repo import GitopsRepo
from dfe_engine.schema.derived_registry import DerivedSchemaRegistry
from dfe_engine.source.registry import SourceRegistry

SOURCES = "/api/v1/sources"

_SOURCE = {
    "source": "fw-logs",
    "description": "firewall logs",
    "match": {"field": "tags.collector.type", "value": "fw-logs"},
}


class _RecordingForge:
    def open_pull_request(self, *, head, base, title, body) -> PullRequest:
        return PullRequest(number=7, url="http://forge/pr/7", branch=head)


@pytest.fixture
def deploy_repo(client, tmp_path):
    """A real local deploy repo with one commit on main.

    ``client`` comes first, so these registries replace the ones the lifespan built.
    """
    repo = GitopsRepo(local_path=str(tmp_path / "deploy"), push=False)
    crud = GitCrud(repo, default_registry())
    crud.put("sources", "seed", {"source": "seed"}, "seed")
    return crud


def _rebind(crud, *, environment: str, mode: str, forge=None) -> None:
    posture = WritePosture(environment=environment, mode=mode, forge=forge)
    for key, registry in (
        ("source", SourceRegistry(crud=crud, posture=posture)),
        ("derived_schema", DerivedSchemaRegistry(crud=crud, posture=posture)),
    ):
        previous = _registries.get(key)
        close = getattr(previous, "close", None)
        if close is not None:
            close()
        _registries[key] = registry


def _on_main(crud, name: str) -> bool:
    return (crud.repo_path / "config" / "sources" / f"{name}.yaml").is_file()


def _create(client, headers):
    return client.post(SOURCES, json=_SOURCE, headers=headers)


def test_a_dev_create_is_saved_and_says_so(client, admin_headers, deploy_repo):
    _rebind(deploy_repo, environment="dev", mode="team")

    resp = _create(client, admin_headers)

    assert resp.status_code == 201, resp.text
    assert resp.json()["review_required"] is False
    assert "X-DFE-Review-Required" not in resp.headers
    assert _on_main(deploy_repo, "fw-logs")


@pytest.mark.parametrize("forge", [True, False], ids=["forge", "no-forge"])
@pytest.mark.parametrize("write", ["create", "update", "enable"])
def test_a_production_team_save_answers_awaiting_review(
    client, admin_headers, deploy_repo, monkeypatch, write, forge
):
    if write != "create":
        _rebind(deploy_repo, environment="dev", mode="team")
        assert _create(client, admin_headers).status_code == 201
    _rebind(
        deploy_repo,
        environment="production",
        mode="team",
        forge=_RecordingForge() if forge else None,
    )
    reconciled: list[str] = []
    real = sources_module.reconcile_derived_apps

    def recording(*args, **kwargs):
        reconciled.append("reconcile")
        return real(*args, **kwargs)

    monkeypatch.setattr(sources_module, "reconcile_derived_apps", recording)
    if write == "create":
        resp = _create(client, admin_headers)
    elif write == "update":
        resp = client.put(
            f"{SOURCES}/fw-logs", json={**_SOURCE, "description": "changed"}, headers=admin_headers
        )
    else:
        resp = client.patch(f"{SOURCES}/fw-logs", json={"enabled": False}, headers=admin_headers)

    # A create that main does not carry yet is accepted, not created.
    assert resp.status_code == (202 if write == "create" else 200), resp.text
    body = resp.json()
    assert body["review_required"] is True
    assert body["message"] == "awaiting review"
    assert body["review_branch"].startswith("dfe/sources/")
    assert resp.headers["X-DFE-Review-Required"] == "true"
    if forge:
        assert body["pr_url"] == "http://forge/pr/7"
        assert resp.headers["X-DFE-PR-Url"] == "http://forge/pr/7"
    else:
        assert body["pr_url"] is None
    # main is unchanged, so there is nothing for the apps to follow yet
    assert reconciled == []
    assert _on_main(deploy_repo, "fw-logs") is (write != "create")


def test_a_delete_held_for_review_keeps_the_topics_and_the_records(
    client, admin_headers, deploy_repo, monkeypatch
):
    _rebind(deploy_repo, environment="dev", mode="team")
    assert _create(client, admin_headers).status_code == 201
    _rebind(deploy_repo, environment="production", mode="team", forge=_RecordingForge())
    removed: list[str] = []

    def remove_topics(source, settings):
        removed.append(source.source)
        return [], []

    monkeypatch.setattr(sources_module, "_remove_source_topics", remove_topics)
    forgotten: list[str] = []
    monkeypatch.setattr(
        SourceRegistry,
        "_forget_deployment_records",
        lambda self, name, store: forgotten.append(name),
    )

    resp = client.delete(f"{SOURCES}/fw-logs", headers=admin_headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["review_required"] is True
    assert resp.json()["pr_url"] == "http://forge/pr/7"
    assert resp.headers["X-DFE-Review-Required"] == "true"
    assert removed == []
    assert forgotten == []
    assert _on_main(deploy_repo, "fw-logs")


_DERIVED = {
    "base": "meta/beats/filebeat",
    "base_version": "1.0.0",
    "current": "1.0.0",
    "versions": {
        "1.0.0": {
            "date": "2026-09-21",
            "summary": "system.auth subset",
            "select": [{"name": "timestamp"}],
        }
    },
}


@pytest.fixture
def base_schema(client, admin_headers):
    """The meta schema the derived one selects from."""
    response = client.post(
        f"/api/v1/schemas/definitions/{_DERIVED['base']}",
        headers=admin_headers,
        json={
            "current": "1.0.0",
            "versions": {
                "1.0.0": {
                    "date": "2026-09-21",
                    "type": "model",
                    "summary": "filebeat base",
                    "columns": [{"name": "timestamp", "type": "timestamp", "_field_type": "base"}],
                }
            },
        },
    )
    assert response.status_code == 201, response.text


@pytest.mark.parametrize("write", ["create", "delete"])
def test_a_derived_schema_write_held_for_review_carries_the_review_headers(
    client, admin_headers, deploy_repo, base_schema, write
):
    path = "/api/v1/schemas/definitions/derived/beats/fw_subset"
    if write == "delete":
        _rebind(deploy_repo, environment="dev", mode="team")
        assert client.post(path, json=_DERIVED, headers=admin_headers).status_code == 201
    _rebind(deploy_repo, environment="production", mode="team", forge=_RecordingForge())

    if write == "create":
        resp = client.post(path, json=_DERIVED, headers=admin_headers)
    else:
        resp = client.delete(path, headers=admin_headers)

    assert resp.status_code in (201, 204), resp.text
    assert resp.headers["X-DFE-Review-Required"] == "true"
    assert resp.headers["X-DFE-PR-Url"] == "http://forge/pr/7"


@pytest.mark.parametrize("action", ["delete", "disable"])
def test_a_bulk_write_held_for_review_is_listed_apart_and_left_alone(
    client, admin_headers, deploy_repo, monkeypatch, action
):
    _rebind(deploy_repo, environment="dev", mode="team")
    assert _create(client, admin_headers).status_code == 201
    _rebind(deploy_repo, environment="production", mode="team", forge=_RecordingForge())
    removed: list[str] = []

    def remove_topics(source, settings):
        removed.append(source.source)
        return [], []

    monkeypatch.setattr(sources_module, "_remove_source_topics", remove_topics)

    resp = client.post(
        f"{SOURCES}/bulk",
        json={"action": action, "sources": [{"source": "fw-logs"}]},
        headers=admin_headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["succeeded"] == []
    assert body["awaiting_review"] == ["fw-logs"]
    assert body["review_required"] is True
    assert body["pr_url"] == "http://forge/pr/7"
    assert resp.headers["X-DFE-Review-Required"] == "true"
    assert removed == []
    assert _on_main(deploy_repo, "fw-logs")


def test_a_catalogue_create_held_for_review_is_listed_apart(client, admin_headers, deploy_repo):
    _rebind(deploy_repo, environment="production", mode="team", forge=_RecordingForge())

    resp = client.post(
        f"{SOURCES}/from-catalogue",
        json={"entries": [{"entry": "okta", "intake": "beats"}]},
        headers=admin_headers,
    )

    assert resp.status_code in (200, 201, 202), resp.text
    body = resp.json()
    assert body["created"] == []
    assert len(body["awaiting_review"]) == 1
    assert body["review_required"] is True
    assert resp.headers["X-DFE-Review-Required"] == "true"
    assert not _on_main(deploy_repo, body["awaiting_review"][0])


def test_an_import_held_for_review_says_so_and_leaves_main(client, admin_headers, deploy_repo):
    _rebind(deploy_repo, environment="dev", mode="team")
    assert _create(client, admin_headers).status_code == 201
    exported = client.get(f"{SOURCES}/fw-logs/export", headers=admin_headers)
    assert exported.status_code == 200, exported.text
    assert client.delete(f"{SOURCES}/fw-logs", headers=admin_headers).status_code == 200
    _rebind(deploy_repo, environment="production", mode="team", forge=_RecordingForge())

    resp = client.post(f"{SOURCES}/import", json=exported.json(), headers=admin_headers)

    assert resp.status_code in (200, 201), resp.text
    assert resp.headers["X-DFE-Review-Required"] == "true"
    assert resp.headers["X-DFE-PR-Url"] == "http://forge/pr/7"
    assert not _on_main(deploy_repo, "fw-logs")


def test_a_deploy_record_held_for_review_leaves_the_apps_as_main_has_them(
    client, admin_headers, deploy_repo, monkeypatch
):
    from dfe_engine.auth.models import AuthContext
    from dfe_engine.source.deployment import SchemaDeployResult

    _rebind(deploy_repo, environment="dev", mode="team")
    assert _create(client, admin_headers).status_code == 201
    _rebind(deploy_repo, environment="production", mode="team", forge=_RecordingForge())
    registry = _registries["source"]
    source = registry.get_source("fw-logs")
    reconciled: list[str] = []
    monkeypatch.setattr(
        sources_module, "reconcile_derived_apps", lambda *a, **k: reconciled.append("r")
    )

    class _Store:
        def save_deploy(self, result, source) -> None:
            return None

    record = sources_module.ReviewRouting()
    apps = sources_module._record_source_deploy(
        _Store(),  # ty: ignore[invalid-argument-type]  # a stand-in carrying only the call this path makes
        SchemaDeployResult(
            source_name="fw-logs",
            version=source.current,
            dry_run=False,
            applied=True,
            create_table="",
        ),
        source,
        "fw-logs",
        source.current,
        AuthContext(user_id="admin"),
        registry,
        None,  # ty: ignore[invalid-argument-type]  # the held path never reads the request
        record,
    )

    assert record.review_required is True
    assert apps.changes == []
    assert reconciled == []
    assert registry.get_source("fw-logs").deployed_version is None
