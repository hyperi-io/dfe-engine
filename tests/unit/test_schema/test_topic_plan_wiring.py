#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_topic_plan_wiring.py
#  Purpose:      Pin what the topic half of the schema plan is rendered for
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The two facts the bootstrap topic set is rendered against: tiering, and size.

Both used to be wrong in the same way -- a value the engine already had was
passed where the renderer wanted a fact about the deployment, so the render
agreed with the config and never with the bus.

``kafka_tiered_storage`` had no way through at all: the renderer grew the kwarg
and the engine's own call sites took a fixed list, so the landing topic was
created without ``remote.storage.enable`` on every deployment that tiers.

``broker_count`` was handed the configured replication factor, so the clamp
compared that factor against itself and could never reduce it.
"""

from __future__ import annotations

import pytest

from dfe_engine.schema.phase import _broker_count
from dfe_engine.schema.plan import build_plan
from dfe_engine.settings import load_settings

TIERED_KEY = "remote.storage.enable"


@pytest.fixture
def settings():
    return load_settings().model_copy(deep=True)


def _landing(plan):
    """The landing topic the bootstrap set declares."""
    landing = [obj for obj in plan.topics() if obj.topic and obj.topic.get("kind") == "landing"]
    assert landing, "the manifest declares no landing topic"
    return landing[0].topic


# -- tiered storage ----------------------------------------------------------


def test_the_landing_topic_carries_the_tiering_key_when_the_deployment_tiers(settings):
    """The kwarg reaches the renderer, which is what the fixed call list blocked."""
    topic = _landing(build_plan(settings=settings, kafka_tiered_storage=True))
    assert topic["config"].get(TIERED_KEY) == "true"


def test_the_tiering_key_is_absent_rather_than_false_when_it_is_off(settings):
    """Absent, not negated: a false here would fight a broker that does tier."""
    topic = _landing(build_plan(settings=settings, kafka_tiered_storage=False))
    assert TIERED_KEY not in topic["config"]


def test_tiered_storage_is_off_unless_the_deployment_says_otherwise(settings):
    assert settings.kafka.tiered_storage is False


def test_the_env_var_sets_the_dial(monkeypatch):
    monkeypatch.setenv("DFE_KAFKA_TIERED_STORAGE", "true")
    assert load_settings().kafka.tiered_storage is True


# -- broker count ------------------------------------------------------------


def test_the_replication_factor_is_clamped_to_the_brokers_there_are(settings):
    """One broker gets one replica, whatever the manifest declares."""
    assert _landing(build_plan(settings=settings, broker_count=1))["replication_factor"] == 1


def test_a_bigger_bus_keeps_the_declared_factor(settings):
    """The clamp reduces; it never raises past what the manifest asked for."""
    declared = _landing(build_plan(settings=settings, broker_count=99))["replication_factor"]
    assert declared > 1
    assert (
        _landing(build_plan(settings=settings, broker_count=declared))["replication_factor"]
        == declared
    )


def test_a_deployment_with_no_bus_never_asks_the_broker(settings, monkeypatch):
    """The lookup costs an admin timeout, so a brokerless tier must not pay it."""
    settings.transport.bus_present = False

    def fail(**_kwargs):
        raise AssertionError("the broker was asked on a deployment with no bus")

    monkeypatch.setattr("dfe_engine.kafka.topics.build_admin", fail)
    assert _broker_count(settings) == 1


def test_the_bootstrap_dial_being_off_also_skips_the_lookup(settings, monkeypatch):
    settings.kafka.bootstrap_topics = False

    def fail(**_kwargs):
        raise AssertionError("the broker was asked with the topic bootstrap off")

    monkeypatch.setattr("dfe_engine.kafka.topics.build_admin", fail)
    assert _broker_count(settings) == 1


def test_an_unreachable_cluster_falls_back_to_one_not_the_configured_factor(settings, monkeypatch):
    """One replica beats a create that succeeds and never becomes Ready."""
    settings.transport.bus_present = True
    settings.kafka.bootstrap_topics = True
    settings.kafka.bootstrap_servers = "broker.invalid:9092"
    settings.kafka.topic_replication_factor = 3

    def unreachable(**_kwargs):
        raise OSError("no route to host")

    monkeypatch.setattr("dfe_engine.kafka.topics.build_admin", unreachable)
    assert _broker_count(settings) == 1


def test_the_broker_count_comes_from_the_cluster(settings, monkeypatch):
    settings.transport.bus_present = True
    settings.kafka.bootstrap_topics = True
    settings.kafka.bootstrap_servers = "broker:9092"
    settings.kafka.topic_replication_factor = 1

    class FakeAdmin:
        def node_count(self, **_kwargs):
            return 5

    monkeypatch.setattr("dfe_engine.kafka.topics.build_admin", lambda **_kwargs: FakeAdmin())
    # 5, not the configured factor of 1: the bus is the fact, the setting is not.
    assert _broker_count(settings) == 5
