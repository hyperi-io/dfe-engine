"""Tests for explicit Kafka topic admin (dfe_engine.kafka.topics)."""

from dfe_engine.kafka.topics import (
    TopicSpec,
    admin_config,
    ensure_topics,
    source_topic_specs,
)
from dfe_engine.source.models import Source, SourceMatch, SourceTransform


class _FakeAdmin:
    """Stands in for TopicAdmin so no test needs a broker."""

    def __init__(self, present=(), fail_on=()):
        self.present = set(present)
        self.fail_on = set(fail_on)
        self.created: list[tuple[str, int, int]] = []

    def list_topic_names(self, *, timeout: float = 10.0) -> set[str]:
        return set(self.present)

    def create(self, name, *, partitions, replication_factor, timeout: float = 30.0) -> None:
        if name in self.fail_on:
            raise RuntimeError("broker said no")
        self.created.append((name, partitions, replication_factor))
        self.present.add(name)


class _UnreachableAdmin:
    def list_topic_names(self, *, timeout: float = 10.0) -> set[str]:
        raise RuntimeError("connection refused")


def _source(name="filebeat", *, transform=True) -> Source:
    return Source(
        source=name,
        match=SourceMatch(field="f", value="v"),
        transform=SourceTransform(engine="vector") if transform else None,
    )


class TestSourceTopicSpecs:
    def test_transforming_source_needs_land_and_load(self):
        specs = source_topic_specs(_source(), partitions=6, replication_factor=3)
        assert [s.name for s in specs] == ["filebeat_land", "filebeat_load"]
        assert all(s.partitions == 6 and s.replication_factor == 3 for s in specs)

    def test_source_without_transform_needs_only_land(self):
        specs = source_topic_specs(
            _source("syslog", transform=False), partitions=3, replication_factor=1
        )
        assert [s.name for s in specs] == ["syslog_land"]


class TestEnsureTopics:
    def test_creates_only_what_is_missing(self):
        admin = _FakeAdmin(present=["filebeat_land"])
        result = ensure_topics(
            source_topic_specs(_source(), partitions=3, replication_factor=1),
            admin=admin,
        )
        assert result.created == ["filebeat_load"]
        assert result.existing == ["filebeat_land"]
        assert result.ok

    def test_is_idempotent(self):
        admin = _FakeAdmin()
        specs = source_topic_specs(_source(), partitions=3, replication_factor=1)
        first = ensure_topics(specs, admin=admin)
        second = ensure_topics(specs, admin=admin)
        assert first.created == ["filebeat_land", "filebeat_load"]
        assert second.created == []
        assert second.existing == ["filebeat_land", "filebeat_load"]

    def test_an_existing_topic_is_never_recreated(self):
        """Partition width of a live topic is not a deploy side effect."""
        admin = _FakeAdmin(present=["filebeat_land", "filebeat_load"])
        ensure_topics(
            source_topic_specs(_source(), partitions=64, replication_factor=9),
            admin=admin,
        )
        assert admin.created == []

    def test_a_failed_topic_does_not_stop_the_others(self):
        admin = _FakeAdmin(fail_on=["filebeat_land"])
        result = ensure_topics(
            source_topic_specs(_source(), partitions=3, replication_factor=1),
            admin=admin,
        )
        assert result.created == ["filebeat_load"]
        assert [name for name, _ in result.failed] == ["filebeat_land"]
        assert not result.ok

    def test_an_unreachable_broker_fails_every_spec_without_raising(self):
        result = ensure_topics(
            source_topic_specs(_source(), partitions=3, replication_factor=1),
            admin=_UnreachableAdmin(),
        )
        assert [name for name, _ in result.failed] == ["filebeat_land", "filebeat_load"]
        assert "connection refused" in result.failed[0][1]

    def test_dry_run_creates_nothing(self):
        admin = _FakeAdmin()
        result = ensure_topics([TopicSpec("t", 3, 1)], admin=admin, dry_run=True)
        assert result.created == ["t"]
        assert admin.created == []

    def test_no_specs_is_a_no_op(self):
        assert ensure_topics([]).created == []


class TestAdminConfig:
    def test_a_scram_provider_carries_credentials(self):
        conf = admin_config(bootstrap="b:9092", provider="strimzi", username="u", password="p")
        assert conf["security.protocol"] == "SASL_SSL"
        assert conf["sasl.mechanisms"] == "SCRAM-SHA-512"
        assert conf["sasl.username"] == "u"
        assert conf["sasl.password"] == "p"

    def test_plaintext_carries_no_sasl_keys(self):
        conf = admin_config(bootstrap="b:9092", provider="plaintext", username="", password="")
        assert conf == {"bootstrap.servers": "b:9092"}
