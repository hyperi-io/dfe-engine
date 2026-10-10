#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_dead_letter_topics.py
#  Purpose:      Dead letters go to topics that exist, or the deployment is not ready
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The engine names the dead-letter topics it creates, and refuses to run without them.

The names are checked against the dfe-schemas manifest the engine renders at
bootstrap, so a rename on either side fails here rather than on a broker that
discards every dead letter.
"""

from collections.abc import Iterable
from importlib import resources

import pytest

from dfe_engine.kafka.topics import TopicAdmin
from dfe_engine.schema.phase import (
    DeadLetterPathError,
    _bootstrap_specs,
    require_dead_letter_topics,
)
from dfe_engine.schema.plan import build_plan
from dfe_engine.services.models.loader import LoaderRoutingConfig
from dfe_engine.services.models.receiver import ReceiverRoutingConfig
from dfe_engine.settings import DFESettings
from dfe_engine.source.models import _topic_policy
from dfe_engine.yaml_utils import yaml_load_string


def _settings(
    *, bus: bool = True, bootstrap_topics: bool = True, size: int | None = None
) -> DFESettings:
    return DFESettings(
        env="dev",
        transport={"default": "bus" if bus else "direct", "bus_present": bus},
        kafka={
            "bootstrap_servers": "broker:9092",
            "bootstrap_topics": bootstrap_topics,
            "topic_max_message_bytes": size,
        },
    )


@pytest.fixture(scope="module")
def plan():
    return build_plan(settings=_settings(), client=None)


@pytest.fixture(scope="module")
def declared(plan) -> set[str]:
    return {r.topic["name"] for r in plan.topics() if r.topic and r.topic.get("kind") == "dlq"}


class _Broker(TopicAdmin):
    """A broker's topic list, with names it refuses to create."""

    def __init__(self, present: Iterable[str] = (), refuse: Iterable[str] = ()) -> None:
        self.present: set[str] = set(present)
        self.refuse: set[str] = set(refuse)
        self.configs: dict[str, dict[str, str]] = {}

    def list_topic_names(self, *, timeout: float = 10.0) -> set[str]:
        return set(self.present)

    def create(
        self,
        name: str,
        *,
        partitions: int,
        replication_factor: int,
        config: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        if name in self.refuse:
            raise RuntimeError("TOPIC_AUTHORIZATION_FAILED")
        self.present.add(name)
        self.configs[name] = dict(config or {})


class _Untouchable(TopicAdmin):
    def __init__(self) -> None:
        pass

    def list_topic_names(self, *, timeout: float = 10.0) -> set[str]:
        raise AssertionError("a deployment with no bus asked the broker")


class TestNames:
    def test_the_receiver_dead_letters_to_a_topic_the_engine_creates(self, declared):
        assert ReceiverRoutingConfig().dlq.topic in declared

    def test_the_loader_dead_letters_to_a_topic_the_engine_creates(self, declared):
        assert LoaderRoutingConfig().dlq.topic in declared

    @pytest.mark.parametrize(
        "name",
        [
            "receiver-default.yaml",
            "receiver-production.yaml",
            "loader-default.yaml",
            "loader-production.yaml",
        ],
    )
    def test_every_shipped_config_names_a_created_topic(self, declared, name):
        text = (resources.files("dfe_engine.services") / "default_configs" / name).read_text()
        assert yaml_load_string(text)["routing"]["dlq"]["topic"] in declared


class TestTheGate:
    def test_a_broker_holding_every_dead_letter_topic_passes(self, plan, declared):
        broker = _Broker(present=declared)

        assert require_dead_letter_topics(
            plan, _settings(), wait_seconds=0, admin=broker
        ) == sorted(declared)

    def test_a_missing_topic_is_created_and_then_on_the_broker(self, plan, declared):
        broker = _Broker()

        require_dead_letter_topics(plan, _settings(), wait_seconds=0, admin=broker)

        assert declared <= broker.present

    def test_a_topic_the_broker_refuses_fails_the_pass_by_name(self, plan):
        broker = _Broker(refuse={"dfe_loader_dlq"})

        with pytest.raises(DeadLetterPathError, match="dfe_loader_dlq: the broker refused it"):
            require_dead_letter_topics(plan, _settings(), wait_seconds=0, admin=broker)

    def test_an_unreachable_broker_fails_the_pass(self, plan):
        # No admin injected: the real admin is built, and the unit suite has no broker.
        with pytest.raises(DeadLetterPathError, match="broker unreachable"):
            require_dead_letter_topics(plan, _settings(), wait_seconds=0)

    @pytest.mark.parametrize(
        "settings",
        [_settings(bus=False), _settings(bootstrap_topics=False)],
        ids=["no-bus", "topic-bootstrap-off"],
    )
    def test_a_deployment_that_records_no_dead_letter_on_kafka_is_not_held(self, plan, settings):
        assert (
            require_dead_letter_topics(plan, settings, wait_seconds=0, admin=_Untouchable()) == []
        )


class TestTheMessageSize:
    """Every bootstrap topic carries the deployment's message size, not the manifest's."""

    def test_each_dead_letter_topic_is_created_at_the_configured_size(self, plan, declared):
        # A managed broker capped at 8 MiB refuses a 16 MiB create, holding the engine NotReady.
        broker = _Broker()
        require_dead_letter_topics(plan, _settings(size=8_388_608), wait_seconds=0, admin=broker)
        assert {name: c["max.message.bytes"] for name, c in broker.configs.items()} == {
            name: "8388608" for name in declared
        }

    def test_the_landing_topic_carries_it_too(self, plan):
        specs = _bootstrap_specs(plan, _settings(size=8_388_608))
        assert {s.name: s.config["max.message.bytes"] for s in specs}["main_land"] == "8388608"

    def test_unset_falls_back_to_the_manifest(self, plan):
        manifest = str(_topic_policy().defaults["max_message_bytes"])
        specs = _bootstrap_specs(plan, _settings())
        assert {s.config["max.message.bytes"] for s in specs} == {manifest}
