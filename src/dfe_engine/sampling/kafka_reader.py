#  Project:      dfe-engine
#  File:         sampling/kafka_reader.py
#  Purpose:      Read sample messages from Kafka for the sampler
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Kafka reads for the sampler (confluent-kafka).

Two entry points:

- ``read_recent`` - a bounded TAIL: seek each partition to
  ``high_watermark - per_partition`` and read forward. Gating is on the
  partition high-watermark (the broker's own "end of topic" signal), never a
  blind sleep, so a caught-up read returns deterministically.
- ``build_source`` - hand logreducer a bounded ``KafkaSource`` (reads from the
  earliest offset, capped by ``max_messages``, never commits).

The engine has no long-lived Kafka consumer; these use throwaway,
non-committing consumer groups so sampling never disturbs real offsets.
"""

from __future__ import annotations

from typing import Any

from .models import SamplerError


def librdkafka_config(kafka_settings: Any) -> dict[str, Any]:
    """Base librdkafka config from KafkaSettings (bootstrap + optional SASL)."""
    conf: dict[str, Any] = {"bootstrap.servers": kafka_settings.bootstrap_servers}
    protocol = (kafka_settings.security_protocol or "").strip()
    if protocol and protocol.upper() != "PLAINTEXT":
        conf["security.protocol"] = protocol
    if kafka_settings.sasl_mechanism:
        conf["sasl.mechanism"] = kafka_settings.sasl_mechanism
        conf["sasl.username"] = kafka_settings.sasl_username
        conf["sasl.password"] = kafka_settings.sasl_password
        # A SASL mechanism with a PLAINTEXT protocol is almost always a
        # misconfig; default to SASL_PLAINTEXT so a dev SCRAM broker still works.
        conf.setdefault("security.protocol", "SASL_PLAINTEXT")
    return conf


def read_recent(
    conf: dict[str, Any],
    topic: str,
    *,
    limit: int,
    group_suffix: str,
    poll_timeout: float = 1.0,
    metadata_timeout: float = 10.0,
    idle_polls: int = 5,
) -> list[str]:
    """Read up to ``limit`` newest messages across all partitions of ``topic``."""
    try:
        from confluent_kafka import Consumer, KafkaError, TopicPartition  # ty: ignore[unresolved-import]
    except ImportError as exc:  # pragma: no cover - confluent-kafka is a hard dep here
        raise SamplerError(
            "Kafka sampling needs confluent-kafka (already a dfe-engine dependency)."
        ) from exc

    consumer = Consumer(
        {
            **conf,
            "group.id": f"dfe-sampler-{group_suffix}",
            "enable.auto.commit": False,
            "enable.partition.eof": True,
            "auto.offset.reset": "latest",
        }
    )
    try:
        meta = consumer.list_topics(topic, timeout=metadata_timeout)
        tmeta = meta.topics.get(topic)
        if tmeta is None or tmeta.error is not None:
            raise SamplerError(f"Kafka topic not found or in error: {topic!r}")
        partitions = list(tmeta.partitions.keys())
        if not partitions:
            return []

        # Ceiling divide so the tail window covers `limit` across all partitions.
        per_partition = max(1, -(-limit // len(partitions)))
        assignments: list[Any] = []
        ends: dict[int, int] = {}
        for p in partitions:
            lo, hi = consumer.get_watermark_offsets(
                TopicPartition(topic, p), timeout=metadata_timeout, cached=False
            )
            ends[p] = hi
            assignments.append(TopicPartition(topic, p, max(lo, hi - per_partition)))
        consumer.assign(assignments)

        lines: list[str] = []
        # A partition whose start is already at its end contributes nothing.
        done: set[int] = {
            p for p, hi in ends.items() if assignments_start_at_end(assignments, p, hi)
        }
        idle = 0
        while len(lines) < limit and len(done) < len(partitions):
            msg = consumer.poll(poll_timeout)
            if msg is None:
                idle += 1
                if idle >= idle_polls:
                    break
                continue
            idle = 0
            err = msg.error()
            if err is not None:
                if err.code() == KafkaError._PARTITION_EOF:
                    done.add(msg.partition())
                continue
            if msg.offset() >= ends.get(msg.partition(), 0) - 1:
                done.add(msg.partition())
            value = msg.value()
            if value is not None:
                lines.append(value.decode("utf-8", errors="replace"))
        # Keep the newest `limit` if we overshot on the last poll batch.
        return lines[-limit:] if len(lines) > limit else lines
    finally:
        consumer.close()


def assignments_start_at_end(assignments: list[Any], partition: int, hi: int) -> bool:
    """True when partition ``partition`` was assigned starting at its high-watermark."""
    for tp in assignments:
        if tp.partition == partition:
            return tp.offset >= hi
    return False


def build_source(conf: dict[str, Any], topic: str, *, max_messages: int, group_suffix: str) -> Any:
    """A bounded logreducer ``KafkaSource`` over ``topic`` (earliest, no commit)."""
    from logreducer.kafka import KafkaSource  # ty: ignore[unresolved-import]

    return KafkaSource(
        conf,
        group_id=f"dfe-sampler-{group_suffix}",
        topics=topic,
        max_messages=max_messages,
    )
