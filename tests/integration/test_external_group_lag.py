#  Project:      dfe-engine
#  File:         tests/integration/test_external_group_lag.py
#  Purpose:      The external transform's consumer-group lag, read off a real broker
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What the engine reports for a customer's transform on a source's landing topic.

The lag is the engine's only view of a pipeline it does not run, so it is read from
a real broker: a group that never committed, one part way through, and one caught up.
"""

import time
import uuid
from collections.abc import Iterator

import pytest
from confluent_kafka import Consumer, KafkaError, KafkaException, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic

from dfe_engine.kafka.topics import build_admin
from dfe_engine.source.external import external_group_lag
from dfe_engine.source.models import Source

RECORDS = 5
COORDINATOR_MOVING = frozenset(
    {
        KafkaError.NOT_COORDINATOR,
        KafkaError.COORDINATOR_NOT_AVAILABLE,
        KafkaError.COORDINATOR_LOAD_IN_PROGRESS,
    }
)


def _commit(bootstrap: str, group: str, topic: str, offset: int, timeout: float = 30.0) -> None:
    """Commit *group*'s position on partition 0, as the customer's pipeline would."""
    consumer = Consumer(
        {"bootstrap.servers": bootstrap, "enable.auto.commit": False, "group.id": group}
    )
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                consumer.commit(offsets=[TopicPartition(topic, 0, offset)], asynchronous=False)
                return
            except KafkaException as exc:
                # The coordinator can move while the broker loads its offsets topic.
                if exc.args[0].code() not in COORDINATOR_MOVING or time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
    finally:
        consumer.close()


@pytest.fixture
def landed(kafka_bootstrap: str) -> Iterator[Source]:
    """An external-transform source whose landing topic holds RECORDS records on partition 0."""
    name = f"ext-{uuid.uuid4().hex[:8]}"
    source = Source.model_validate(
        {
            "source": name,
            "match": {"field": "_source", "value": name},
            "transform": {"engine": "external"},
        }
    )
    admin = AdminClient({"bootstrap.servers": kafka_bootstrap})
    admin.create_topics([NewTopic(source.topic_land, num_partitions=1, replication_factor=1)])[
        source.topic_land
    ].result(30)
    producer = Producer({"bootstrap.servers": kafka_bootstrap, "acks": "all"})
    for index in range(RECORDS):
        producer.produce(source.topic_land, value=f'{{"n": {index}}}'.encode(), partition=0)
    assert producer.flush(30) == 0
    yield source
    admin.delete_topics([source.topic_land])[source.topic_land].result(30)


@pytest.mark.parametrize(
    ("committed_to", "lag", "committed"),
    [
        pytest.param(None, RECORDS, False, id="never-committed"),
        pytest.param(2, RECORDS - 2, True, id="part-way"),
        pytest.param(RECORDS, 0, True, id="caught-up"),
    ],
)
def test_the_lag_is_what_the_group_has_not_committed_past(
    kafka_bootstrap, landed, committed_to, lag, committed
):
    group = f"dfe-external-{landed.source}"
    if committed_to is not None:
        _commit(kafka_bootstrap, group, landed.topic_land, committed_to)

    read = external_group_lag(landed, build_admin(bootstrap=kafka_bootstrap, provider="plaintext"))

    assert (read.group, read.topic) == (group, landed.topic_land)
    assert (read.lag, read.committed) == (lag, committed)
    assert read.members == 0
