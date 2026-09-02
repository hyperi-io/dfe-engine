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
