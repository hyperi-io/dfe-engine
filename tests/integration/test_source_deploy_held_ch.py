#  Project:      dfe-engine
#  File:         tests/integration/test_source_deploy_held_ch.py
#  Purpose:      A production+team source deploy changes nothing until its review merges
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A production+team source deploy, held for review, against a real ClickHouse, Kafka and deploy repo.

The deploy sends its record to a review branch and changes no table, topic or app.
Merging the branch puts the version on main, and the next merged-deploy pass runs
the deploy an unheld one runs. A branch closed unmerged never reaches main, so its
table stays as it was. Merging and closing are done on the local deploy repo, as a
forge does on the remote.
"""

import uuid
from types import SimpleNamespace

import pytest
from confluent_kafka.admin import AdminClient
from dulwich import porcelain
from dulwich.repo import Repo

from dfe_engine.appmgmt import derived, instances
from dfe_engine.gitcrud.forge import PullRequest
from dfe_engine.orgs.tenancy import Tenancy
from dfe_engine.schema.retention import request_converge
from dfe_engine.source.deployment import SourceDeploymentStore
from tests.support.engine_stack import converged

pytestmark = [pytest.mark.integration, pytest.mark.own_clickhouse]

REVIEW_PREFIX = b"refs/heads/dfe/"
LOADER = "dfe-loader"
GIT_IDENTITY = b"reviewer <reviewer@example.com>"


class _RecordingForge:
    """Opens each review PR as a forge would, so the apps reconcile can route its writes."""

    def open_pull_request(self, *, head, base, title, body) -> PullRequest:
        return PullRequest(number=1, url=f"http://forge/pr/{head}", branch=head)


def _body(name: str, header: dict) -> dict:
    return {
        "display_name": name,
        "enabled": True,
        "header": header,
        "match": {"field": "tags.collector.type", "value": name},
        "schema": {"meta_schema": "meta/syslog"},
        "source": name,
    }


def _review_branches(stack: SimpleNamespace) -> list[bytes]:
    with Repo(str(stack.crud.repo.path)) as repo:
        return sorted(ref for ref in repo.refs.allkeys() if ref.startswith(REVIEW_PREFIX))


def _merge_reviews(stack: SimpleNamespace) -> None:
    """Merge every open review branch into main and delete it, as the forge does on merge."""
    repo = stack.crud.repo
    with repo._write_lock, repo._lock:
        for ref in _review_branches(stack):
            branch = ref.removeprefix(b"refs/heads/")
            porcelain.merge(str(repo.path), branch, author=GIT_IDENTITY, committer=GIT_IDENTITY)
            porcelain.branch_delete(str(repo.path), branch)


def _close_reviews(stack: SimpleNamespace) -> None:
    """Delete every open review branch unmerged, as a forge does on a closed PR."""
    repo = stack.crud.repo
    with repo._write_lock, repo._lock:
        for ref in _review_branches(stack):
            porcelain.branch_delete(str(repo.path), ref.removeprefix(b"refs/heads/"))


def _merged_pass(stack: SimpleNamespace) -> None:
    trigger = stack.app.state.merged_deploy_converge
    trigger.request()
    assert trigger.wait_idle(120.0), "the merged-deploy pass was still running after 120 s"


def _columns(ch_client, database: str, table: str) -> set[str]:
    rows = ch_client.query(
        "SELECT name FROM system.columns WHERE database = {db:String} AND table = {t:String}",
        parameters={"db": database, "t": table},
    ).result_rows
    return {str(row[0]) for row in rows}


def _ddl(text: str) -> list[str]:
    """A CREATE TABLE without the line stamping when it was generated."""
    return [line for line in text.splitlines() if not line.startswith("-- Generated at:")]


def _topics(bootstrap: str) -> set[str]:
    return set(AdminClient({"bootstrap.servers": bootstrap}).list_topics(timeout=10).topics)


def _store(stack: SimpleNamespace) -> SourceDeploymentStore:
    return SourceDeploymentStore.from_settings(stack.app.state.settings)


def _apps_plan(stack: SimpleNamespace) -> list[str]:
    """What the apps still need to match the sources on main. Empty is in step."""
    from dfe_engine.api.deps import get_source_registry

    state = stack.app.state
    changes = derived.plan(
        stack.crud,
        get_source_registry(),
        state.settings,
        tenancy=Tenancy.read(state.org_registry),
    )
    return [change.describe() for change in changes]


def _run_loader(stack: SimpleNamespace) -> instances.AppInstance:
    """Deploy a loader whose routing is in step with main, as an operator's merged PR leaves it."""
    loader = instances.instance_of(LOADER, "default")
    stack.crud.put(
        instances.HELMVARS_CLASS, loader.overlay_name, instances.initial_overlay(loader), "test"
    )
    state = stack.app.state
    from dfe_engine.api.deps import get_source_registry

    derived.reconcile(
        stack.crud, get_source_registry(), state.settings, tenancy=Tenancy.read(state.org_registry)
    )
    assert _apps_plan(stack) == []
    return loader


def _create(stack: SimpleNamespace, name: str, header: dict) -> None:
    created = stack.client.post("/api/v1/sources", headers=stack.headers, json=_body(name, header))
    assert created.status_code == 202, created.text
    _merge_reviews(stack)


def _deploy(stack: SimpleNamespace, name: str) -> dict:
    resp = stack.client.post(f"/api/v1/sources/{name}/deploy", headers=stack.headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _assert_held(stack: SimpleNamespace, body: dict, name: str, applied_version: str | None):
    """The deploy went to review, and this engine records it unapplied."""
    assert body["review_required"] is True
    assert body["review_branch"]
    assert body["applied"] is False
    assert body["statements_applied"] == 0
    assert body["topics_ensured"] == []
    doc = _store(stack).load_deploy_document(name)
    assert doc is not None
    assert doc.versions[body["version"]].applied is False
    assert doc.deployed_version == applied_version


def test_a_held_deploy_changes_nothing_until_its_review_merges(
    ch_client, held_kafka_stack, kafka_bootstrap
):
    stack = held_kafka_stack
    stack.app.state.forge = _RecordingForge()
    name = f"held-{uuid.uuid4().hex[:8]}"
    _create(stack, name, {"type": "timeseries", "version": "1.0.0"})
    loader = _run_loader(stack)

    body = _deploy(stack, name)

    _assert_held(stack, body, name, applied_version=None)
    _merged_pass(stack)
    assert _columns(ch_client, stack.database, name) == set()
    assert f"{name}_land" not in _topics(kafka_bootstrap)

    _merge_reviews(stack)
    assert _apps_plan(stack), "main records the deploy, so the apps are behind it"
    _merged_pass(stack)

    # The pass ran the deploy an unheld one runs: table, topics, apps, then the record.
    doc = _store(stack).load_deploy_document(name)
    assert doc is not None
    assert doc.deployed_version == body["version"]
    record = doc.versions[body["version"]]
    assert record.applied is True
    assert _ddl(record.create_table) == _ddl(body["create_table"])
    assert _columns(ch_client, stack.database, name)
    assert f"{name}_land" in _topics(kafka_bootstrap)
    assert f"{name}_land" in record.topics_ensured
    # The apps reconcile's writes go to review here too, and merged they leave nothing to do.
    _merge_reviews(stack)
    assert _apps_plan(stack) == []
    assert name in str(instances.read_overlay(stack.crud, loader))
    # Applied once: the next pass finds nothing to deploy.
    statements = record.statements_applied
    _merged_pass(stack)
    again = _store(stack).load_deploy(name, body["version"])
    assert again is not None
    assert again.statements_applied == statements


def test_a_review_closed_unmerged_leaves_the_table_untouched(ch_client, held_kafka_stack):
    stack = held_kafka_stack
    stack.app.state.forge = _RecordingForge()
    name = f"closed-{uuid.uuid4().hex[:8]}"
    _create(stack, name, {"type": "minimal", "version": "1.0.0"})
    first = _deploy(stack, name)
    _merge_reviews(stack)
    _merged_pass(stack)
    _merge_reviews(stack)
    before = _columns(ch_client, stack.database, name)
    assert before

    # A header with more columns is a new major version.
    updated = stack.client.put(
        f"/api/v1/sources/{name}",
        headers=stack.headers,
        json=_body(name, {"type": "timeseries", "version": "1.0.0"}),
    )
    assert updated.status_code == 200, updated.text
    _merge_reviews(stack)
    second = _deploy(stack, name)
    assert second["version"] != first["version"]
    assert second["columns_added"], "the held version adds columns the live table lacks"
    _assert_held(stack, second, name, applied_version=first["version"])

    # Neither pass takes the version main does not record: the default-TTL pass reads this engine's applied version.
    request_converge(stack.app.state)
    converged(stack)
    _merged_pass(stack)
    assert _columns(ch_client, stack.database, name) == before

    _close_reviews(stack)
    _merged_pass(stack)
    request_converge(stack.app.state)
    converged(stack)

    assert _columns(ch_client, stack.database, name) == before
    doc = _store(stack).load_deploy_document(name)
    assert doc is not None
    assert doc.deployed_version == first["version"]
    assert doc.versions[second["version"]].applied is False
