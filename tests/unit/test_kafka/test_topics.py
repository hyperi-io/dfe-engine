"""Tests for explicit Kafka topic admin (dfe_engine.kafka.topics)."""

from types import SimpleNamespace

import pytest

from dfe_engine.kafka.contract import KafkaContractError
from dfe_engine.kafka.topics import (
    TopicAdmin,
    TopicSpec,
    admin_config,
    ensure_all_source_topics,
    ensure_topics,
    remove_topics,
    source_topic_names,
    source_topic_specs,
    specs_for_sources,
    topic_status,
    topics_managed,
    topics_managed_at_startup,
    update_topics,
)
from dfe_engine.settings import KafkaSettings, TransportSettings
from dfe_engine.source.models import Source, SourceFetcher, SourceMatch, SourceTransform


def _settings(**kafka) -> SimpleNamespace:
    return SimpleNamespace(kafka=KafkaSettings(**kafka))


def _deployment(*, bus=True, **kafka) -> SimpleNamespace:
    """Settings with both dials the topic hooks read."""
    return SimpleNamespace(
        kafka=KafkaSettings(**kafka),
        transport=TransportSettings(default="bus" if bus else "direct", bus_present=bus),
    )


class _FakeAdmin:
    """Stands in for TopicAdmin so no test needs a broker."""

    def __init__(self, present=(), fail_on=(), shapes=None, configs=None):
        self.present = set(present)
        self.fail_on = set(fail_on)
        # name -> (partitions, replication_factor) for a topic the broker holds.
        self.shapes: dict[str, tuple[int, int]] = dict(shapes or {})
        self.configs: dict[str, dict[str, str]] = dict(configs or {})
        self.created: list[tuple[str, int, int]] = []
        self.created_config: dict[str, dict[str, str]] = {}
        self.deleted: list[str] = []
        self.altered: dict[str, dict[str, str]] = {}
        self.widened: dict[str, int] = {}

    def list_topic_names(self, *, timeout: float = 10.0) -> set[str]:
        return set(self.present)

    def create(
        self, name, *, partitions, replication_factor, config=None, timeout: float = 30.0
    ) -> None:
        if name in self.fail_on:
            raise RuntimeError("broker said no")
        self.created.append((name, partitions, replication_factor))
        self.created_config[name] = dict(config or {})
        self.shapes[name] = (partitions, replication_factor)
        self.configs[name] = dict(config or {})
        self.present.add(name)

    def delete(self, name, *, timeout: float = 30.0) -> None:
        if name in self.fail_on:
            raise RuntimeError("broker said no")
        self.deleted.append(name)
        self.present.discard(name)

    def describe_shape(self, name, *, timeout: float = 10.0) -> tuple[int, int]:
        if name in self.fail_on:
            raise RuntimeError("broker said no")
        return self.shapes.get(name, (3, 1))

    def describe_config(self, name, *, timeout: float = 30.0) -> dict[str, str]:
        if name in self.fail_on:
            raise RuntimeError("broker said no")
        return dict(self.configs.get(name, {}))

    def alter_config(self, name, changes, *, timeout: float = 30.0) -> None:
        if name in self.fail_on:
            raise RuntimeError("broker said no")
        self.altered[name] = dict(changes)
        self.configs.setdefault(name, {}).update(changes)

    def widen_partitions(self, name, *, total, timeout: float = 30.0) -> None:
        if name in self.fail_on:
            raise RuntimeError("broker said no")
        self.widened[name] = total
        partitions, rf = self.shapes.get(name, (3, 1))
        self.shapes[name] = (total, rf)


class _UnreachableAdmin:
    def list_topic_names(self, *, timeout: float = 10.0) -> set[str]:
        raise RuntimeError("connection refused")


def _main_lander(name="aws-cloudtrail", *, transform=False) -> Source:
    """A fetcher source whose records land on the shared topic it does not own."""
    return Source(
        source=name,
        fetcher=SourceFetcher(source_type="aws", topic="main"),
        transform=SourceTransform(engine="vector") if transform else None,
    )


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

    def test_a_main_landing_source_claims_no_landing_topic(self):
        # The landing source owns main_land; claiming it here would render it
        # with this source's partition count instead.
        specs = source_topic_specs(_main_lander(), partitions=3, replication_factor=1)
        assert specs == []

    def test_a_main_landing_source_still_gets_its_own_load_topic(self):
        specs = source_topic_specs(_main_lander(transform=True), partitions=3, replication_factor=1)
        assert [s.name for s in specs] == ["aws-cloudtrail_load"]

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

    def test_the_spec_config_reaches_the_new_topic(self):
        """Retention set on the deployment has to land AT creation.

        Creating the topic bare and altering it afterwards leaves a window where
        the broker default applies to whatever already arrived.
        """
        admin = _FakeAdmin()
        ensure_topics([TopicSpec("t", 3, 1, {"retention.ms": "86400000"})], admin=admin)
        assert admin.created_config["t"] == {"retention.ms": "86400000"}


class TestTopicsManaged:
    """One reader for "does this deployment manage its topics", four callers."""

    def test_a_bus_deployment_manages_them(self):
        assert topics_managed(_deployment())

    def test_a_brokerless_deployment_does_not(self):
        assert not topics_managed(_deployment(bus=False))

    def test_the_operator_override_wins_either_way(self):
        assert not topics_managed(_deployment(ensure_topics=False))
        assert topics_managed(_deployment(bus=False, ensure_topics=True))

    def test_the_startup_pass_needs_the_dial_set_explicitly(self):
        assert not topics_managed_at_startup(_deployment())
        assert not topics_managed_at_startup(_deployment(ensure_topics=False))
        assert topics_managed_at_startup(_deployment(ensure_topics=True))


class TestSpecsForSources:
    def test_it_names_the_owner_of_every_topic(self):
        specs, owners = specs_for_sources(
            [_source(), _source("syslog", transform=False)], _deployment()
        )

        assert [s.name for s in specs] == ["filebeat_land", "filebeat_load", "syslog_land"]
        assert owners == {
            "filebeat_land": "filebeat",
            "filebeat_load": "filebeat",
            "syslog_land": "syslog",
        }

    def test_the_deployment_topic_config_rides_every_spec(self):
        specs, _ = specs_for_sources([_source()], _deployment(topic_cleanup_policy="compact"))

        assert all(s.config == {"cleanup.policy": "compact"} for s in specs)


class TestEnsureAllSourceTopics:
    """The startup pass: every source's topics, not only the ones redeployed."""

    def test_it_covers_every_source(self):
        admin = _FakeAdmin()

        result = ensure_all_source_topics(
            [_source(), _source("syslog", transform=False)],
            _deployment(ensure_topics=True),
            admin=admin,
        )

        assert result.created == ["filebeat_land", "filebeat_load", "syslog_land"]

    def test_an_unset_dial_reaches_no_broker(self):
        """Boot-time, so it runs only where an operator turned it on.

        Following ``transport.bus_present`` here would make every boot of a
        deployment that was never told its broker address spend the admin
        timeout before giving up.
        """
        admin = _FakeAdmin()

        result = ensure_all_source_topics([_source()], _deployment(), admin=admin)

        assert (result.created, admin.created) == ([], [])

    def test_the_off_switch_reaches_no_broker_either(self):
        admin = _FakeAdmin()

        result = ensure_all_source_topics(
            [_source()], _deployment(ensure_topics=False), admin=admin
        )

        assert (result.created, admin.created) == ([], [])

    def test_no_sources_is_a_no_op(self):
        assert ensure_all_source_topics([], _deployment(ensure_topics=True)).created == []


class TestSourceTopicNames:
    """What a delete removes: everything the source's deploys ever created."""

    def test_a_transforming_source_owns_both(self):
        assert source_topic_names(_source()) == ["filebeat_land", "filebeat_load"]

    def test_a_source_that_never_transformed_owns_only_its_landing_topic(self):
        assert source_topic_names(_source("syslog", transform=False)) == ["syslog_land"]

    def test_removing_a_main_landing_source_leaves_the_shared_topic(self):
        # Deleting main_land here would take the receiver's default flow and
        # every other source landing on it down with this one source.
        assert source_topic_names(_main_lander()) == []

    def test_removing_a_main_landing_source_still_removes_its_load_topic(self):
        assert source_topic_names(_main_lander(transform=True)) == ["aws-cloudtrail_load"]

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


class TestTopicStatus:
    """A read that changes nothing and says what differs."""

    def test_a_missing_topic_reads_as_drift_not_an_error(self):
        result = topic_status([TopicSpec("filebeat_land", 3, 1)], admin=_FakeAdmin())

        assert result.reachable
        state = result.topics[0]
        assert (state.exists, state.drift) == (False, ["topic does not exist"])

    def test_a_matching_topic_has_no_drift(self):
        admin = _FakeAdmin(present=["filebeat_land"], shapes={"filebeat_land": (3, 1)})

        result = topic_status([TopicSpec("filebeat_land", 3, 1)], admin=admin)

        assert result.topics[0].drift == []
        assert (result.topics[0].partitions, result.topics[0].replication_factor) == (3, 1)

    def test_it_names_every_kind_of_drift(self):
        admin = _FakeAdmin(
            present=["filebeat_land"],
            shapes={"filebeat_land": (3, 1)},
            configs={"filebeat_land": {"retention.ms": "604800000"}},
        )

        result = topic_status(
            [TopicSpec("filebeat_land", 6, 3, {"retention.ms": "86400000"})], admin=admin
        )

        drift = result.topics[0].drift
        assert any("partitions 3, wanted 6" in line for line in drift)
        assert any("replication factor 1, wanted 3" in line for line in drift)
        assert any("retention.ms 604800000, wanted 86400000" in line for line in drift)

    def test_a_config_the_spec_says_nothing_about_is_not_drift(self):
        # An operator's own tuning must not read as something to converge away.
        admin = _FakeAdmin(
            present=["filebeat_land"],
            shapes={"filebeat_land": (3, 1)},
            configs={"filebeat_land": {"max.message.bytes": "2097152"}},
        )

        result = topic_status([TopicSpec("filebeat_land", 3, 1)], admin=admin)

        assert result.topics[0].drift == []

    def test_an_unreachable_broker_is_not_reported_as_missing_topics(self):
        """ "Absent" and "could not look" are different answers to different problems."""
        result = topic_status([TopicSpec("filebeat_land", 3, 1)], admin=_UnreachableAdmin())

        assert not result.reachable
        assert "connection refused" in result.error
        assert result.topics == []

    def test_it_carries_the_owning_source(self):
        specs, owners = specs_for_sources([_source()], _deployment())

        result = topic_status(specs, sources=owners, admin=_FakeAdmin())

        assert {state.source for state in result.topics} == {"filebeat"}

    def test_no_specs_is_a_no_op(self):
        assert topic_status([]).topics == []


class TestUpdateTopics:
    """Converge: alter the configs the spec names, widen partitions, refuse the rest."""

    def test_a_drifted_config_is_altered(self):
        admin = _FakeAdmin(
            present=["t"], shapes={"t": (3, 1)}, configs={"t": {"retention.ms": "604800000"}}
        )

        result = update_topics([TopicSpec("t", 3, 1, {"retention.ms": "86400000"})], admin=admin)

        assert result.altered == ["t"]
        assert admin.altered == {"t": {"retention.ms": "86400000"}}

    def test_only_the_drifted_keys_are_sent(self):
        admin = _FakeAdmin(
            present=["t"],
            shapes={"t": (3, 1)},
            configs={"t": {"retention.ms": "86400000", "cleanup.policy": "delete"}},
        )

        update_topics(
            [TopicSpec("t", 3, 1, {"retention.ms": "86400000", "cleanup.policy": "compact"})],
            admin=admin,
        )

        assert admin.altered == {"t": {"cleanup.policy": "compact"}}

    def test_a_partition_increase_is_applied(self):
        admin = _FakeAdmin(present=["t"], shapes={"t": (3, 1)})

        result = update_topics([TopicSpec("t", 6, 1)], admin=admin)

        assert result.widened == ["t"]
        assert admin.widened == {"t": 6}

    def test_a_partition_decrease_is_refused_not_attempted(self):
        # Kafka has no shrink, and the records on the partitions it would drop
        # have nowhere to go.
        admin = _FakeAdmin(present=["t"], shapes={"t": (6, 1)})

        result = update_topics([TopicSpec("t", 3, 1)], admin=admin)

        assert [name for name, _ in result.refused] == ["t"]
        assert "cannot drop a partition" in result.refused[0][1]
        assert admin.widened == {}

    def test_a_replication_factor_change_is_refused(self):
        admin = _FakeAdmin(present=["t"], shapes={"t": (3, 1)})

        result = update_topics([TopicSpec("t", 3, 3)], admin=admin)

        assert "partition reassignment" in result.refused[0][1]

    def test_a_topic_that_does_not_exist_is_absent_not_created(self):
        admin = _FakeAdmin()

        result = update_topics([TopicSpec("t", 3, 1)], admin=admin)

        assert (result.absent, admin.created) == (["t"], [])

    def test_a_matching_topic_is_unchanged(self):
        admin = _FakeAdmin(present=["t"], shapes={"t": (3, 1)})

        result = update_topics([TopicSpec("t", 3, 1)], admin=admin)

        assert result.unchanged == ["t"]
        assert (admin.altered, admin.widened) == ({}, {})

    def test_it_is_idempotent(self):
        admin = _FakeAdmin(
            present=["t"], shapes={"t": (3, 1)}, configs={"t": {"retention.ms": "1"}}
        )
        spec = TopicSpec("t", 6, 1, {"retention.ms": "86400000"})

        update_topics([spec], admin=admin)
        second = update_topics([spec], admin=admin)

        assert second.unchanged == ["t"]

    def test_dry_run_reports_without_touching_the_broker(self):
        admin = _FakeAdmin(
            present=["t"], shapes={"t": (3, 1)}, configs={"t": {"retention.ms": "1"}}
        )

        result = update_topics(
            [TopicSpec("t", 6, 1, {"retention.ms": "86400000"})], admin=admin, dry_run=True
        )

        assert (result.altered, result.widened) == (["t"], ["t"])
        assert (admin.altered, admin.widened) == ({}, {})

    def test_an_unreachable_broker_fails_every_spec_without_raising(self):
        result = update_topics([TopicSpec("t", 3, 1)], admin=_UnreachableAdmin())

        assert [name for name, _ in result.failed] == ["t"]
        assert "broker unreachable" in result.failed[0][1]

    def test_no_specs_is_a_no_op(self):
        assert update_topics([]).unchanged == []


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
            topic = kw.get("topic")
            if topic is not None:
                partitions = {
                    0: SimpleNamespace(replicas=[1, 2]),
                    1: SimpleNamespace(replicas=[2, 1]),
                }
                return SimpleNamespace(topics={topic: SimpleNamespace(partitions=partitions)})
            return SimpleNamespace(topics={"a": object(), "b": object()})

        def create_topics(self, new_topics, **kw):
            self.calls.append(("create_topics", tuple(new_topics), kw))
            return {t.topic: _DoneFuture() for t in new_topics}

        def delete_topics(self, topics, **kw):
            self.calls.append(("delete_topics", tuple(topics), kw))
            return {name: _DoneFuture() for name in topics}

        def describe_configs(self, resources, **kw):
            self.calls.append(("describe_configs", tuple(resources), kw))
            from confluent_kafka.admin import ConfigEntry

            entries = {
                "retention.ms": ConfigEntry("retention.ms", "604800000"),
                "compression.type": ConfigEntry("compression.type", None),
            }
            return {resources[0]: _DoneFuture(entries)}

        def incremental_alter_configs(self, resources, **kw):
            self.calls.append(("incremental_alter_configs", tuple(resources), kw))
            return {resources[0]: _DoneFuture()}

        def create_partitions(self, new_partitions, **kw):
            self.calls.append(("create_partitions", tuple(new_partitions), kw))
            return {p.topic: _DoneFuture() for p in new_partitions}

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

    def test_create_passes_the_config_through(self, monkeypatch):
        admin, created = self._admin(monkeypatch)
        admin.create("t", partitions=3, replication_factor=1, config={"retention.ms": "1"})
        _, topics, _ = created["client"].calls[0]
        assert topics[0].config == {"retention.ms": "1"}

    def test_describe_shape_counts_partitions_and_replicas(self, monkeypatch):
        admin, created = self._admin(monkeypatch)
        assert admin.describe_shape("t") == (2, 2)
        assert created["client"].calls[0] == ("list_topics", (), {"topic": "t", "timeout": 10.0})

    def test_describe_config_renders_a_null_value_as_empty(self, monkeypatch):
        """librdkafka reports an unset config with value None; str(None) is a lie."""
        admin, _ = self._admin(monkeypatch)
        assert admin.describe_config("t") == {
            "retention.ms": "604800000",
            "compression.type": "",
        }

    def test_alter_config_uses_the_incremental_call(self, monkeypatch):
        """A whole-resource alter resets every config it is not given."""
        admin, created = self._admin(monkeypatch)
        admin.alter_config("t", {"retention.ms": "1"})
        name, resources, kwargs = created["client"].calls[0]
        assert name == "incremental_alter_configs"
        assert kwargs == {"request_timeout": 30.0}
        assert resources[0].name == "t"

    def test_widen_partitions_sends_the_new_total(self, monkeypatch):
        admin, created = self._admin(monkeypatch)
        admin.widen_partitions("t", total=6)
        name, partitions, kwargs = created["client"].calls[0]
        assert name == "create_partitions"
        assert kwargs == {"request_timeout": 30.0}
        assert partitions[0].topic == "t"

    def test_the_real_newpartitions_takes_the_total(self):
        """Pure constructor, no broker. Catches a NewPartitions signature change."""
        from confluent_kafka.admin import NewPartitions

        assert NewPartitions("t", 6).topic == "t"


class _DoneFuture:
    def __init__(self, value=None):
        self._value = value

    def result(self, *_a, **_kw):
        return self._value


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

    def test_the_in_cluster_listener_keeps_scram_and_drops_the_tls(self):
        """dfe-engine#332: the :9092 listener the deploy charts stand up."""
        conf = admin_config(
            bootstrap="b:9092", provider="strimzi-no-tls", username="u", password="p"
        )
        assert conf["security.protocol"] == "SASL_PLAINTEXT"
        assert conf["sasl.mechanisms"] == "SCRAM-SHA-512"
        assert conf["sasl.password"] == "p"

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
