"""Imperative operations that Argo CD cannot handle.

ClickHouse DDL execution and Kafka topic creation are side-effectful
operations that must be run explicitly. The compiler produces the
*what* (DDL statements, topic specs); this module executes them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from scalo.logger import logger

from dfe_engine.helm.environment import EnvironmentConfig
from dfe_engine.helm.models import CompilationResult


@dataclass
class OperationResult:
    """Result of executing imperative operations."""

    ddl_executed: list[str] = field(default_factory=list)
    ddl_failed: list[tuple[str, str]] = field(default_factory=list)
    topics_created: list[str] = field(default_factory=list)
    topics_failed: list[tuple[str, str]] = field(default_factory=list)


class ImperativeOperations:
    """Executes side-effectful operations from a CompilationResult.

    Separated from the compiler because compilation is pure (deterministic,
    no side effects) while these operations mutate external systems.

    Args:
        environment: Target deployment environment with connection details.
    """

    def __init__(self, environment: EnvironmentConfig) -> None:
        self._env = environment

    def execute_ddl(
        self,
        statements: list[str],
        *,
        dry_run: bool = False,
    ) -> OperationResult:
        """Execute ClickHouse DDL statements.

        Args:
            statements: CREATE TABLE / ALTER TABLE SQL strings.
            dry_run: If True, log statements without executing.

        Returns:
            OperationResult with executed/failed lists.
        """
        result = OperationResult()

        if dry_run:
            for stmt in statements:
                logger.info(f"[dry-run] DDL: {stmt[:120]}...")
                result.ddl_executed.append(stmt)
            return result

        try:
            import clickhouse_connect

            client = clickhouse_connect.get_client(
                host=self._env.clickhouse.hosts[0].split(":")[0],
                port=int(self._env.clickhouse.hosts[0].split(":")[-1]),
                database=self._env.clickhouse.database,
                username=self._env.clickhouse.username,
                password=self._env.clickhouse.password.get_secret_value(),
                secure=self._env.clickhouse.secure,
            )

            for stmt in statements:
                try:
                    client.command(stmt)
                    result.ddl_executed.append(stmt)
                    logger.info(f"DDL executed: {stmt[:80]}...")
                except Exception as e:
                    result.ddl_failed.append((stmt, str(e)))
                    logger.error(f"DDL failed: {stmt[:80]}... -- {e}")

        except ImportError:
            for stmt in statements:
                result.ddl_failed.append((stmt, "clickhouse-connect not installed"))
        except Exception as e:
            for stmt in statements:
                result.ddl_failed.append((stmt, f"Connection failed: {e}"))

        return result

    def create_topics(
        self,
        topics: list[dict[str, Any]],
        *,
        dry_run: bool = False,
    ) -> OperationResult:
        """Create Kafka topics.

        Args:
            topics: List of topic spec dicts (name, partitions, replication_factor).
            dry_run: If True, log topics without creating.

        Returns:
            OperationResult whose ``topics_created`` holds every topic that now
            exists, created here or already present - a re-run is a no-op, not a
            list of failures. Only a topic that could not be created is failed.
        """
        from dfe_engine.kafka.topics import TopicSpec, ensure_topics

        specs = [
            TopicSpec(
                name=t["name"],
                partitions=t.get("partitions", 3),
                replication_factor=t.get("replication_factor", 1),
            )
            for t in topics
        ]
        ensured = ensure_topics(
            specs,
            bootstrap=",".join(self._env.kafka.bootstrap_servers),
            dry_run=dry_run,
        )

        return OperationResult(
            topics_created=ensured.created + ensured.existing,
            topics_failed=ensured.failed,
        )

    def execute_all(
        self,
        compilation: CompilationResult,
        *,
        dry_run: bool = False,
    ) -> OperationResult:
        """Execute all imperative operations from a compilation result.

        Args:
            compilation: Result from HelmValuesCompiler.compile_all().
            dry_run: If True, log without executing.

        Returns:
            Combined OperationResult.
        """
        ddl_result = self.execute_ddl(compilation.ddl_statements, dry_run=dry_run)
        topic_result = self.create_topics(compilation.kafka_topics, dry_run=dry_run)

        return OperationResult(
            ddl_executed=ddl_result.ddl_executed,
            ddl_failed=ddl_result.ddl_failed,
            topics_created=topic_result.topics_created,
            topics_failed=topic_result.topics_failed,
        )
