"""Tests for explicit Kafka topic admin (dfe_engine.kafka.topics)."""

from types import SimpleNamespace

import pytest

from dfe_engine.kafka.contract import KafkaContractError
from dfe_engine.kafka.topics import (
    TopicAdmin,
    TopicSpec,
    admin_config,
    ensure_topics,
    remove_topics,
    source_topic_names,
    source_topic_specs,
)
from dfe_engine.settings import KafkaSettings
from dfe_engine.source.models import Source, SourceMatch, SourceTransform


def _settings(**kafka) -> SimpleNamespace:
    return SimpleNamespace(kafka=KafkaSettings(**kafka))


class _FakeAdmin:
    """Stands in for TopicAdmin so no test needs a broker."""

    def __init__(self, present=(), fail_on=()):
        self.present = set(present)
        self.fail_on = set(fail_on)
        self.created: list[tuple[str, int, int]] = []
        self.deleted: list[str] = []

    def list_topic_names(self, *, timeout: float = 10.0) -> set[str]:
        return set(self.present)

    def create(self, name, *, partitions, replication_factor, timeout: float = 30.0) -> None:
        if name in self.fail_on:
            raise RuntimeError("broker said no")
        self.created.append((name, partitions, replication_factor))
        self.present.add(name)

    def delete(self, name, *, timeout: float = 30.0) -> None:
        if name in self.fail_on:
            raise RuntimeError("broker said no")
        self.deleted.append(name)
        self.present.discard(name)


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

    def test_the_version_being_deployed_decides_the_load_topic(self):
        """A release that ADDS a transform must get its _load topic.

        ``Source.transform`` reads the version already DEPLOYED, so without the
        version_id the new transform is judged against the old snapshot and
        produces to a topic nobody created.
        """
        dt = "2026-01-01T00:00:00Z"
        source = Source.model_validate(
            {
                "source": "syslog",
                "deployed_version": "1.0.0",
                "current": "2.0.0",
                "versions": {
                    "1.0.0": {"date_time": dt, "match": {"field": "f", "value": "v"}},
                    "2.0.0": {
                        "date_time": dt,
                        "match": {"field": "f", "value": "v"},
                        "transform": {"engine": "vector"},
                    },
                },
            }
        )
        deployed = source_topic_specs(source, partitions=3, replication_factor=1)
        assert [s.name for s in deployed] == ["syslog_land"]

        deploying = source_topic_specs(
            source, partitions=3, replication_factor=1, version_id="2.0.0"
        )
        assert [s.name for s in deploying] == ["syslog_land", "syslog_load"]


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


class TestSourceTopicNames:
    """What a delete removes: everything the source's deploys ever created."""

    def test_a_transforming_source_owns_both(self):
        assert source_topic_names(_source()) == ["filebeat_land", "filebeat_load"]

    def test_a_source_that_never_transformed_owns_only_its_landing_topic(self):
        assert source_topic_names(_source("syslog", transform=False)) == ["syslog_land"]

    def test_a_version_that_dropped_its_transform_still_owns_the_load_topic(self):
        # The _load topic was created when the transform was deployed, and the
        # version that removed it did not take the topic with it.
        dt = "2026-01-01T00:00:00Z"
        source = Source.model_validate(
            {
                "source": "syslog",
                "deployed_version": "2.0.0",
                "current": "2.0.0",
                "versions": {
                    "1.0.0": {
                        "date_time": dt,
                        "match": {"field": "f", "value": "v"},
                        "transform": {"engine": "vector"},
                    },
                    "2.0.0": {"date_time": dt, "match": {"field": "f", "value": "v"}},
                },
            }
        )

        assert source_topic_names(source) == ["syslog_land", "syslog_load"]


class TestRemoveTopics:
    def test_it_deletes_what_the_broker_lists(self):
        admin = _FakeAdmin(present=["filebeat_land", "filebeat_load", "other_land"])

        result = remove_topics(source_topic_names(_source()), admin=admin)

        assert result.removed == ["filebeat_land", "filebeat_load"]
        assert admin.present == {"other_land"}
        assert result.ok

    def test_a_topic_the_broker_does_not_list_is_absent_not_an_error(self):
        admin = _FakeAdmin(present=["filebeat_land"])

        result = remove_topics(source_topic_names(_source()), admin=admin)

        assert (result.removed, result.absent) == (["filebeat_land"], ["filebeat_load"])
        assert result.ok

    def test_it_is_idempotent(self):
        admin = _FakeAdmin(present=["filebeat_land", "filebeat_load"])
        names = source_topic_names(_source())

        remove_topics(names, admin=admin)
        second = remove_topics(names, admin=admin)

        assert second.removed == []
        assert second.absent == ["filebeat_land", "filebeat_load"]

    def test_a_failed_delete_does_not_stop_the_others(self):
        admin = _FakeAdmin(present=["filebeat_land", "filebeat_load"], fail_on=["filebeat_land"])

        result = remove_topics(source_topic_names(_source()), admin=admin)

        assert result.removed == ["filebeat_load"]
        assert [name for name, _ in result.failed] == ["filebeat_land"]
        assert not result.ok

    def test_an_unreachable_broker_fails_every_name_without_raising(self):
        result = remove_topics(["filebeat_land"], admin=_UnreachableAdmin())

        assert [name for name, _ in result.failed] == ["filebeat_land"]
        assert "connection refused" in result.failed[0][1]

    def test_dry_run_deletes_nothing(self):
        admin = _FakeAdmin(present=["t"])

        result = remove_topics(["t"], admin=admin, dry_run=True)

        assert result.removed == ["t"]
        assert admin.deleted == []

    def test_no_names_is_a_no_op(self):
        assert remove_topics([]).removed == []


class TestTopicAdminAdapter:
    """The one place our code meets confluent_kafka.

    Every other test substitutes at the TopicAdmin level, so nothing else here
    would notice the adapter calling the library with a keyword it does not take.
    """

    class _RecordingClient:
        def __init__(self, config):
            self.config = config
            self.calls: list[tuple[str, tuple, dict]] = []

        def list_topics(self, **kw):
            self.calls.append(("list_topics", (), kw))
            return SimpleNamespace(topics={"a": object(), "b": object()})

        def create_topics(self, new_topics, **kw):
            self.calls.append(("create_topics", tuple(new_topics), kw))
            return {t.topic: _DoneFuture() for t in new_topics}

        def delete_topics(self, topics, **kw):
            self.calls.append(("delete_topics", tuple(topics), kw))
            return {name: _DoneFuture() for name in topics}

    def _admin(self, monkeypatch):
        created = {}

        def _factory(config):
            created["client"] = self._RecordingClient(config)
            return created["client"]

        monkeypatch.setattr("dfe_engine.kafka.topics.AdminClient", _factory)
        return TopicAdmin({"bootstrap.servers": "b:9092"}), created

    def test_list_uses_the_timeout_keyword(self, monkeypatch):
        admin, created = self._admin(monkeypatch)
        assert admin.list_topic_names() == {"a", "b"}
        assert created["client"].calls[0] == ("list_topics", (), {"timeout": 10.0})

    def test_create_uses_request_timeout_not_timeout(self, monkeypatch):
        """`timeout=` is a TypeError on the real AdminClient.create_topics."""
        admin, created = self._admin(monkeypatch)
        admin.create("t", partitions=6, replication_factor=3)
        name, topics, kwargs = created["client"].calls[0]
        assert name == "create_topics"
        assert kwargs == {"request_timeout": 30.0}
        assert (topics[0].topic, topics[0].num_partitions, topics[0].replication_factor) == (
            "t",
            6,
            3,
        )

    def test_delete_uses_request_timeout(self, monkeypatch):
        admin, created = self._admin(monkeypatch)
        admin.delete("t")
        assert created["client"].calls[0] == ("delete_topics", ("t",), {"request_timeout": 30.0})

    def test_the_real_newtopic_takes_the_keywords_we_pass(self):
        """Pure constructor, no broker. Catches a NewTopic signature change."""
        from confluent_kafka.admin import NewTopic

        topic = NewTopic("t", num_partitions=6, replication_factor=3)
        assert (topic.topic, topic.num_partitions, topic.replication_factor) == ("t", 6, 3)
        with pytest.raises(TypeError):
            NewTopic("t", partitions=6)


class _DoneFuture:
    def result(self, *_a, **_kw):
        return None


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

    def test_a_mechanism_without_a_sasl_transport_is_refused(self):
        """librdkafka would warn and connect UNAUTHENTICATED; that must not pass silently."""
        settings = _settings(
            bootstrap_servers="b:9092",
            security_protocol="PLAINTEXT",
            sasl_mechanism="SCRAM-SHA-512",
            sasl_username="u",
            sasl_password="p",
        )
        with pytest.raises(KafkaContractError, match="needs a SASL transport"):
            admin_config(settings=settings)


class TestCredentialsFollowTheirBroker:
    """A password issued for one broker must not travel to a different address."""

    def _configured(self):
        return _settings(
            provider="strimzi",
            bootstrap_servers="configured:9092",
            sasl_username="u",
            sasl_password="p",
        )

    def test_the_configured_broker_still_gets_its_credentials(self):
        conf = admin_config(bootstrap="configured:9092", settings=self._configured())
        assert conf["sasl.password"] == "p"

    def test_no_override_at_all_still_gets_its_credentials(self):
        conf = admin_config(settings=self._configured())
        assert conf["bootstrap.servers"] == "configured:9092"
        assert conf["sasl.password"] == "p"

    def test_a_redirected_bootstrap_gets_no_inherited_secret(self):
        conf = admin_config(bootstrap="somewhere-else:9092", settings=self._configured())
        assert conf == {"bootstrap.servers": "somewhere-else:9092"}
        assert "sasl.password" not in conf

    def test_an_empty_override_falls_back_and_is_not_a_redirect(self):
        """`",".join([])` from an environment with no brokers must not look like one."""
        conf = admin_config(bootstrap="", settings=self._configured())
        assert conf["bootstrap.servers"] == "configured:9092"
        assert conf["sasl.password"] == "p"

    def test_a_redirect_that_brings_its_own_credentials_keeps_them(self):
        conf = admin_config(
            bootstrap="somewhere-else:9092",
            provider="strimzi",
            username="other-user",
            password="other-pass",
            settings=self._configured(),
        )
        assert conf["sasl.username"] == "other-user"
        assert conf["sasl.password"] == "other-pass"
