#  Project:      dfe-engine
#  File:         tests/unit/test_auth/test_group_seed.py
#  Purpose:      Group files written into the group store at boot
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""``seed_groups`` over the YAML group store: what a seed file sets, and what it leaves."""

import json
from pathlib import Path

import pytest
from prometheus_client.parser import text_string_to_metric_families
from scalo.metrics import create_metrics

from dfe_engine.auth.admin_retirement import ADMIN_ROLE
from dfe_engine.auth.breakglass import GROUP as ADMIN_GROUP
from dfe_engine.auth.groups import GROUPS_SKIPPED, GroupMetrics, GroupStore, seed_groups
from dfe_engine.yaml_utils import yaml_dump


@pytest.fixture
def seed(tmp_path: Path) -> Path:
    directory = tmp_path / "seed"
    directory.mkdir()
    return directory


@pytest.fixture
def store(tmp_path: Path) -> GroupStore:
    return GroupStore(tmp_path / "groups")


def _write(seed: Path, name: str, fields: dict) -> None:
    yaml_dump(fields, seed / f"{name}.yaml")


def _skips(manager) -> dict[str, float]:
    return {
        sample.labels["reason"]: sample.value
        for family in text_string_to_metric_families(manager.metrics_text)
        for sample in family.samples
        if sample.name == GROUPS_SKIPPED
    }


def test_a_group_no_store_holds_is_created_from_its_file(store, seed):
    _write(seed, "okta-analysts", {"roles": ["data_viewer"], "org_ids": ["acme"]})

    assert seed_groups(store, seed) == ["okta-analysts"]

    group = store.get("okta-analysts")
    assert group is not None
    assert (group.roles, group.org_ids, group.members) == (["data_viewer"], ["acme"], [])


def test_the_file_wins_on_every_field_it_carries(store, seed):
    store.create("analysts", ["infra_viewer"], "edited through the API")
    _write(seed, "analysts", {"roles": ["data_viewer"], "description": "Analysts"})

    seed_groups(store, seed)

    group = store.get("analysts")
    assert group is not None
    assert (group.roles, group.description) == (["data_viewer"], "Analysts")


def test_members_a_file_leaves_out_survive_the_next_boot(store, seed):
    store.create("analysts", ["data_viewer"], members=["alice", "bob"])
    _write(seed, "analysts", {"roles": ["data_viewer"]})

    seed_groups(store, seed)

    group = store.get("analysts")
    assert group is not None
    assert group.members == ["alice", "bob"]


def test_members_a_file_names_replace_the_stored_ones(store, seed):
    store.create("analysts", ["data_viewer"], members=["alice", "bob"])
    _write(seed, "analysts", {"members": ["carol"]})

    seed_groups(store, seed)

    group = store.get("analysts")
    assert group is not None
    assert group.members == ["carol"]


def test_attributes_a_file_carries_replace_the_stored_ones(store, seed):
    store.create("analysts", ["data_viewer"])
    store.set_attributes("analysts", {"colour": "blue"})
    _write(seed, "analysts", {"attributes": {"tier": "gold"}})

    seed_groups(store, seed)

    group = store.get("analysts")
    assert group is not None
    assert group.attributes == {"tier": "gold"}


def test_a_stored_group_no_file_names_is_left_alone(store, seed):
    store.create("local", ["data_viewer"], members=["alice"])
    _write(seed, "okta-analysts", {"roles": ["data_viewer"]})

    seed_groups(store, seed)

    assert [group.name for group in store.list()] == ["local", "okta-analysts"]
    local = store.get("local")
    assert local is not None
    assert local.members == ["alice"]


def test_a_file_that_drops_a_recovery_account_from_the_admin_group_is_refused(
    store, seed, audit_events
):
    store.create(ADMIN_GROUP, [ADMIN_ROLE], members=["admin"])
    _write(seed, ADMIN_GROUP, {"members": []})
    _write(seed, "analysts", {"roles": ["data_viewer"]})

    assert seed_groups(store, seed) == ["analysts"]

    admins = store.get(ADMIN_GROUP)
    assert admins is not None
    assert admins.members == ["admin"]
    (line,) = [
        e for e in audit_events if e["event"] == "group seed file skipped: the store refused it"
    ]
    assert (line["file"], line["error_type"]) == (f"{ADMIN_GROUP}.yaml", "ProtectedAccountError")


@pytest.mark.parametrize(
    ("reason", "content"),
    [
        pytest.param("not_a_mapping", b"- data_viewer\n", id="a-list-at-the-top"),
        pytest.param("invalid", b"scope: nowhere\n", id="a-scope-the-model-refuses"),
        pytest.param("unreadable", b"roles: [data_viewer\n", id="yaml-that-does-not-parse"),
        pytest.param("unreadable", b"\xff\xfe roles: []\n", id="bytes-that-are-not-utf-8"),
    ],
)
def test_a_file_that_does_not_load_is_skipped_and_counted(store, seed, reason, content):
    manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
    (seed / "broken.yaml").write_bytes(content)
    _write(seed, "analysts", {"roles": ["data_viewer"]})

    seeded = seed_groups(store, seed, metrics=GroupMetrics(manager))

    assert seeded == ["analysts"]
    assert store.get("broken") is None
    assert _skips(manager) == {reason: 1.0}


def test_the_log_names_a_skipped_file_never_its_values(store, seed, audit_events):
    (seed / "broken.yaml").write_text("scope: sentinel-scope-8814\n", encoding="utf-8")

    seed_groups(store, seed)

    (line,) = [e for e in audit_events if e["event"] == "group seed file skipped"]
    assert (line["file"], line["reason"], line["error_type"]) == (
        "broken.yaml",
        "invalid",
        "ValidationError",
    )
    assert "sentinel-scope-8814" not in json.dumps(audit_events, default=str)


def test_a_file_not_named_for_a_group_is_skipped(store, seed):
    _write(seed, "-leading-dash", {"roles": ["data_viewer"]})

    assert seed_groups(store, seed) == []
    assert store.list() == []


def test_an_absent_directory_seeds_nothing(store, tmp_path):
    assert seed_groups(store, tmp_path / "absent") == []
    assert store.list() == []
