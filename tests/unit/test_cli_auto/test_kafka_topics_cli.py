#  Project:      dfe-engine
#  File:         tests/unit/test_cli_auto/test_kafka_topics_cli.py
#  Purpose:      Tests for `dfe kafka topics` - explicit topic CRUD
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Tests for `dfe kafka topics` (dfe-engine#97).

scalo.kafka.admin.KafkaAdmin does not (yet) expose topic create/delete/list -
only config-alter operations on topics that already exist (see the module
note in ``cli/auto/kafka.py``). So instead of mocking KafkaAdmin, these tests
patch the CLI module's ``build_admin`` binding with a small in-memory fake
implementing the same ``list_topic_names``/``create``/``delete`` surface
``TopicAdmin`` provides - no live broker needed.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

import dfe_engine.cli.auto.kafka as kafka_module
from dfe_engine.cli.auto.kafka import kafka_group


class FakeAdmin:
    """In-memory stand-in for ``_TopicAdmin`` (list/create/delete only)."""

    def __init__(self, existing: set[str] | None = None) -> None:
        self.existing: set[str] = set(existing or ())
        self.created: list[tuple[str, int, int]] = []
        self.deleted: list[str] = []

    def list_topic_names(self, *, timeout: float = 10.0) -> set[str]:
        return set(self.existing)

    def create(
        self, name: str, *, partitions: int, replication_factor: int, timeout: float = 30.0
    ) -> None:
        self.created.append((name, partitions, replication_factor))
        self.existing.add(name)

    def delete(self, name: str, *, timeout: float = 30.0) -> None:
        self.deleted.append(name)
        self.existing.discard(name)


@pytest.fixture
def fake_admin(monkeypatch: pytest.MonkeyPatch) -> FakeAdmin:
    admin = FakeAdmin()
    monkeypatch.setattr(kafka_module, "build_admin", lambda **kwargs: admin)
    return admin


# --- list -------------------------------------------------------------------


def test_list_prints_topics_excluding_internal(fake_admin: FakeAdmin) -> None:
    fake_admin.existing = {"alpha_land", "beta_load", "__consumer_offsets"}
    r = CliRunner().invoke(kafka_group, ["topics", "list"])
    assert r.exit_code == 0, r.output
    assert "alpha_land" in r.output
    assert "beta_load" in r.output
    assert "__consumer_offsets" not in r.output


def test_list_include_internal(fake_admin: FakeAdmin) -> None:
    fake_admin.existing = {"alpha_land", "__consumer_offsets"}
    r = CliRunner().invoke(kafka_group, ["topics", "list", "--internal"])
    assert r.exit_code == 0, r.output
    assert "__consumer_offsets" in r.output


def test_list_no_topics(fake_admin: FakeAdmin) -> None:
    r = CliRunner().invoke(kafka_group, ["topics", "list"])
    assert r.exit_code == 0, r.output
    assert "(no topics)" in r.output


# --- ensure -------------------------------------------------------------------


def test_ensure_creates_only_missing(fake_admin: FakeAdmin) -> None:
    fake_admin.existing = {"alpha_land"}
    r = CliRunner().invoke(
        kafka_group,
        ["topics", "ensure", "--topic", "alpha_land", "--topic", "alpha_load"],
    )
    assert r.exit_code == 0, r.output
    assert "exists" in r.output
    assert "alpha_land" in r.output
    assert "created" in r.output
    assert "alpha_load" in r.output
    assert fake_admin.created == [("alpha_load", 3, 1)]


def test_ensure_dry_run_creates_nothing(fake_admin: FakeAdmin) -> None:
    fake_admin.existing = {"alpha_land"}
    r = CliRunner().invoke(
        kafka_group,
        ["topics", "ensure", "--topic", "alpha_land", "--topic", "alpha_load", "--dry-run"],
    )
    assert r.exit_code == 0, r.output
    assert "would create" in r.output
    assert "alpha_load" in r.output
    assert fake_admin.created == []


def test_ensure_all_exist_creates_nothing(fake_admin: FakeAdmin) -> None:
    fake_admin.existing = {"alpha_land", "alpha_load"}
    r = CliRunner().invoke(
        kafka_group,
        ["topics", "ensure", "--topic", "alpha_land", "--topic", "alpha_load"],
    )
    assert r.exit_code == 0, r.output
    assert "nothing to create" in r.output
    assert fake_admin.created == []


def test_ensure_honours_partitions_and_replication_flags(fake_admin: FakeAdmin) -> None:
    r = CliRunner().invoke(
        kafka_group,
        [
            "topics",
            "ensure",
            "--topic",
            "gamma_land",
            "--partitions",
            "6",
            "--replication-factor",
            "3",
        ],
    )
    assert r.exit_code == 0, r.output
    assert fake_admin.created == [("gamma_land", 6, 3)]


def test_ensure_without_topic_or_sources_errors(
    fake_admin: FakeAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No --topic given and no source->topic mapping available: ensure must
    # NOT invent one - it requires an explicit --topic and says so.
    monkeypatch.setattr(kafka_module, "_derive_source_topics", list)
    r = CliRunner().invoke(kafka_group, ["topics", "ensure"])
    assert r.exit_code != 0
    assert "--topic" in r.output
    assert fake_admin.created == []


def test_ensure_falls_back_to_derived_source_topics(
    fake_admin: FakeAdmin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        kafka_module, "_derive_source_topics", lambda: ["filebeat_land", "filebeat_load"]
    )
    r = CliRunner().invoke(kafka_group, ["topics", "ensure"])
    assert r.exit_code == 0, r.output
    assert fake_admin.created == [("filebeat_land", 3, 1), ("filebeat_load", 3, 1)]


# --- delete -------------------------------------------------------------------


def test_delete_confirms_then_deletes(fake_admin: FakeAdmin) -> None:
    fake_admin.existing = {"alpha_land"}
    r = CliRunner().invoke(kafka_group, ["topics", "delete", "alpha_land"], input="y\n")
    assert r.exit_code == 0, r.output
    assert fake_admin.deleted == ["alpha_land"]
    assert "deleted" in r.output


def test_delete_aborts_on_no(fake_admin: FakeAdmin) -> None:
    fake_admin.existing = {"alpha_land"}
    r = CliRunner().invoke(kafka_group, ["topics", "delete", "alpha_land"], input="n\n")
    assert r.exit_code != 0
    assert fake_admin.deleted == []


def test_delete_yes_flag_skips_confirm(fake_admin: FakeAdmin) -> None:
    fake_admin.existing = {"alpha_land"}
    r = CliRunner().invoke(kafka_group, ["topics", "delete", "alpha_land", "-y"])
    assert r.exit_code == 0, r.output
    assert fake_admin.deleted == ["alpha_land"]


def test_admin_config_derives_mechanism_from_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """With nothing passed, the broker config comes from settings and the
    provider derives the mechanism."""
    from dfe_engine import settings as settings_module
    from dfe_engine.kafka.topics import admin_config

    monkeypatch.setattr(
        settings_module,
        "get_settings",
        lambda: settings_module.DFESettings(
            env="test",
            kafka=settings_module.KafkaSettings(provider="redpanda", bootstrap_servers="b:9093"),
        ),
    )
    conf = admin_config(bootstrap=None, provider=None, username=None, password=None)
    assert conf["bootstrap.servers"] == "b:9093"
    assert conf["security.protocol"] == "SASL_SSL"
    assert conf["sasl.mechanisms"] == "SCRAM-SHA-512"
