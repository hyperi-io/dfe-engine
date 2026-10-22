#  Project:      dfe-engine
#  File:         tests/integration/test_docustore_group_seed.py
#  Purpose:      Group files seeded into the document store by engine replicas at boot
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The ``groups/`` auth seed on the document store backend, across two engine replicas.

The replicas share the harness's document store (``mongo_uri``) and one seed
directory, as replicas mounting one ConfigMap do (:mod:`tests.support.engine_replicas`).
"""

import functools
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dfe_engine.yaml_utils import yaml_dump
from tests.support.accounts import admin_on_its_own_password
from tests.support.engine_replicas import Deployment, engine_replicas
from tests.support.races import race, raised

pytestmark = pytest.mark.integration

_GROUPS = "/api/v1/auth/groups"


@pytest.fixture
def deployment(mongo_uri: str, tmp_path: Path) -> Iterator[Deployment]:
    """Replicas started through it stop, and its database is dropped, at teardown."""
    with engine_replicas(root=tmp_path, uri=mongo_uri) as deployed:
        yield deployed


def _seed(deployment: Deployment, name: str, **fields: Any) -> None:
    yaml_dump(fields, deployment.groups_seed / f"{name}.yaml")


def _listed(replica: SimpleNamespace) -> list[str]:
    resp = replica.client.get(_GROUPS, params={"per_page": -1}, headers=replica.headers)
    assert resp.status_code == 200, resp.text
    return [item["name"] for item in resp.json()["items"]]


def _group(replica: SimpleNamespace, name: str) -> dict[str, Any]:
    resp = replica.client.get(f"{_GROUPS}/{name}", headers=replica.headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestGroupSeed:
    """Every boot writes the seeded groups into the store, the file winning on what it carries."""

    @pytest.mark.parametrize("round_", range(3))
    def test_two_replicas_booting_at_once_leave_one_of_each_group(self, deployment, round_):
        _seed(deployment, "okta-analysts", roles=["data_analyst"], description="Analysts")
        _seed(deployment, "okta-viewers", roles=["data_viewer"], description="Viewers")
        apps = [deployment.app("a"), deployment.app("b")]

        results = race([functools.partial(deployment.start, app) for app in apps])

        assert raised(results) == []
        for app in apps:
            admin_on_its_own_password(app)
        replicas = [deployment.replica(name) for name in ("c", "d")]
        for replica in replicas:
            listed = _listed(replica)
            assert [listed.count(name) for name in ("okta-analysts", "okta-viewers")] == [1, 1]
            viewers = _group(replica, "okta-viewers")
            assert (viewers["roles"], viewers["description"]) == (["data_viewer"], "Viewers")

    def test_the_next_boot_puts_back_seeded_fields_and_keeps_api_members(self, deployment):
        _seed(deployment, "okta-analysts", roles=["data_analyst"], description="Analysts")
        first = deployment.replica("a")
        edited = first.client.put(
            f"{_GROUPS}/okta-analysts",
            json={"roles": ["data_viewer"], "description": "Edited"},
            headers=first.headers,
        )
        assert edited.status_code == 200, edited.text
        added = first.client.post(
            f"{_GROUPS}/okta-analysts/members", json={"username": "alice"}, headers=first.headers
        )
        assert added.status_code == 200, added.text

        deployment.replica("b")

        stored = _group(first, "okta-analysts")
        assert (stored["roles"], stored["description"], stored["members"]) == (
            ["data_analyst"],
            "Analysts",
            ["alice"],
        )

    def test_a_seeded_member_holds_the_group_on_its_own_account(self, deployment):
        first = deployment.replica("a")
        first.app.state.account_store.create("alice", "alice-password-1")
        _seed(deployment, "okta-analysts", roles=["data_analyst"], members=["alice"])

        second = deployment.replica("b")

        account = second.app.state.account_store.get("alice")
        assert account is not None
        assert account.groups == ["okta-analysts"]
