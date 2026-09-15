"""Tests for Helm imperative operations."""

from dfe_engine.helm.environment import (
    ClickHouseEnvironment,
    EnvironmentConfig,
    KafkaEnvironment,
)
from dfe_engine.helm.models import CompilationResult
from dfe_engine.helm.operations import ImperativeOperations
from dfe_engine.kafka.topics import TopicSpec, ensure_topics


class _Unreachable:
    def list_topic_names(self, *, timeout: float = 10.0) -> set[str]:
        raise RuntimeError("connection refused")


def _env():
    return EnvironmentConfig(
        name="test",
        kafka=KafkaEnvironment(bootstrap_servers=["kafka:9092"]),
        clickhouse=ClickHouseEnvironment(hosts=["ch:9000"], database="dfe"),
    )


class TestDryRun:
    def test_ddl_dry_run(self):
        ops = ImperativeOperations(_env())
        result = ops.execute_ddl(
            ["CREATE TABLE dfe.test (id UInt64) ENGINE = MergeTree"],
            dry_run=True,
        )
        assert len(result.ddl_executed) == 1
        assert result.ddl_failed == []

    def test_topics_dry_run(self):
        ops = ImperativeOperations(_env())
        result = ops.create_topics(
            [{"name": "events_land", "partitions": 3, "replication_factor": 1}],
            dry_run=True,
        )
        assert result.topics_created == ["events_land"]
        assert result.topics_failed == []

    def test_execute_all_dry_run(self):
        ops = ImperativeOperations(_env())
        compilation = CompilationResult(
            ddl_statements=["CREATE TABLE ..."],
            kafka_topics=[{"name": "t1", "partitions": 3}],
        )
        result = ops.execute_all(compilation, dry_run=True)
        assert len(result.ddl_executed) == 1
        assert len(result.topics_created) == 1


class TestMissingDependencies:
    def test_ddl_without_clickhouse_connect(self):
        ops = ImperativeOperations(_env())
        # Non-dry-run will fail gracefully if clickhouse-connect is not configured
        result = ops.execute_ddl(["CREATE TABLE test (id UInt64)"])
        # Should either execute or fail gracefully — no exceptions
        assert len(result.ddl_executed) + len(result.ddl_failed) == 1

    def test_topics_against_an_unreachable_broker(self):
        """Every requested topic is accounted for; nothing raises."""
        result = ensure_topics([TopicSpec("test_topic", 3, 1)], admin=_Unreachable())
        assert [name for name, _ in result.failed] == ["test_topic"]


class _FakeAdmin:
    def __init__(self, present=()):
        self.present = set(present)
        self.created: list[str] = []

    def list_topic_names(self, *, timeout: float = 10.0) -> set[str]:
        return set(self.present)

    def create(
        self, name, *, partitions, replication_factor, config=None, timeout: float = 30.0
    ) -> None:
        self.created.append(name)
        self.present.add(name)


class TestRealRun:
    """dry_run=True returns before any of the wiring below is reached, so these are
    the only tests that prove create_topics is connected to anything."""

    def _patched(self, monkeypatch, admin):
        seen = {}

        def _build_admin(*, bootstrap=None, settings=None, **_kw):
            seen["bootstrap"] = bootstrap
            return admin

        monkeypatch.setattr("dfe_engine.kafka.topics.build_admin", _build_admin)
        return seen

    def test_the_environment_broker_is_the_one_contacted(self, monkeypatch):
        admin = _FakeAdmin()
        seen = self._patched(monkeypatch, admin)
        ImperativeOperations(_env()).create_topics(
            [{"name": "events_land", "partitions": 3, "replication_factor": 1}]
        )
        assert seen["bootstrap"] == "kafka:9092"

    def test_the_requested_width_reaches_the_broker(self, monkeypatch):
        recorded = {}

        class _Recording(_FakeAdmin):
            def create(self, name, *, partitions, replication_factor, config=None, timeout=30.0):
                recorded[name] = (partitions, replication_factor)
                super().create(name, partitions=partitions, replication_factor=replication_factor)

        self._patched(monkeypatch, _Recording())
        ImperativeOperations(_env()).create_topics(
            [{"name": "events_land", "partitions": 6, "replication_factor": 3}]
        )
        assert recorded == {"events_land": (6, 3)}

    def test_an_already_present_topic_counts_as_done_not_failed(self, monkeypatch):
        """A re-run is a no-op. Before this, create over an existing topic errored."""
        self._patched(monkeypatch, _FakeAdmin(present=["events_land"]))
        result = ImperativeOperations(_env()).create_topics(
            [
                {"name": "events_land", "partitions": 3, "replication_factor": 1},
                {"name": "events_load", "partitions": 3, "replication_factor": 1},
            ]
        )
        assert sorted(result.topics_created) == ["events_land", "events_load"]
        assert result.topics_failed == []

    def test_a_missing_width_falls_back_to_the_defaults(self, monkeypatch):
        recorded = {}

        class _Recording(_FakeAdmin):
            def create(self, name, *, partitions, replication_factor, config=None, timeout=30.0):
                recorded[name] = (partitions, replication_factor)
                super().create(name, partitions=partitions, replication_factor=replication_factor)

        self._patched(monkeypatch, _Recording())
        ImperativeOperations(_env()).create_topics([{"name": "bare"}])
        assert recorded == {"bare": (3, 1)}
