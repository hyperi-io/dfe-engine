#  Project:      dfe-engine
#  File:         tests/integration/test_source_drops_its_transform.py
#  Purpose:      A source switched to no transform, deployed through the API on a real Kafka
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A source switched to no transform, deployed through the engine API against a real broker.

The loader stops reading a source's ``_land`` topic while its ``_load`` one exists.
The deploy of the version without the transform goes ahead and reports ``_load``
pending, because the transform instance it removes may still be writing. A later
apps reconcile, or the engine's next start, deletes the topic once the transform's
consumer group holds no live member and the loader's group has read everything on it.
"""

import time
import uuid
from types import SimpleNamespace

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic

from dfe_engine.appmgmt import instances

LOADER_GROUP = "dfe-loader"
RECORDS = 3
TRANSFORM_APP = "dfe-transform-vector"
COORDINATOR_MOVING = frozenset(
    {
        KafkaError.NOT_COORDINATOR,
        KafkaError.COORDINATOR_NOT_AVAILABLE,
        KafkaError.COORDINATOR_LOAD_IN_PROGRESS,
    }
)


def _body(name: str, *, transform: bool) -> dict:
    """A source write body, with the transform chosen or cleared by null."""
    return {
        "display_name": name,
        "enabled": True,
        "header": {"type": "timeseries", "version": "1.0.0"},
        "match": {"field": "tags.collector.type", "value": name},
        "schema": {"meta_schema": "meta/syslog"},
        "source": name,
        "transform": (
            {"engine": "vector", "config_file": f"/etc/vector/{name}.yaml"} if transform else None
        ),
    }


def _topics(bootstrap: str) -> set[str]:
    return set(AdminClient({"bootstrap.servers": bootstrap}).list_topics(timeout=10).topics)


def _produce(bootstrap: str, topic: str) -> None:
    """What the transform wrote before it was dropped, all on partition 0."""
    producer = Producer({"bootstrap.servers": bootstrap, "acks": "all"})
    for index in range(RECORDS):
        producer.produce(topic, value=f'{{"n": {index}}}'.encode(), partition=0)
    assert producer.flush(30) == 0


def _commit(consumer: Consumer, offsets: list[TopicPartition], timeout: float = 30.0) -> None:
    """Commit *offsets* for *consumer*'s group, waiting out a coordinator still moving."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            consumer.commit(offsets=offsets, asynchronous=False)
            return
        except KafkaException as exc:
            # The coordinator can move while the broker loads its offsets topic.
            if exc.args[0].code() not in COORDINATOR_MOVING or time.monotonic() > deadline:
                raise
            time.sleep(0.5)


def _loader_reads_to(bootstrap: str, topic: str, offset: int) -> None:
    """Commit the loader group's position on partition 0, as the loader does after a flush."""
    consumer = Consumer(
        {"bootstrap.servers": bootstrap, "enable.auto.commit": False, "group.id": LOADER_GROUP}
    )
    try:
        _commit(consumer, [TopicPartition(topic, 0, offset)])
    finally:
        consumer.close()


def _transform_joins(bootstrap: str, name: str, timeout: float = 60.0) -> Consumer:
    """The transform's consumer on ``_land``, in the group its instance runs in, once it is assigned."""
    assigned: list[TopicPartition] = []
    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "enable.auto.commit": False,
            "group.id": _transform_group(name),
        }
    )
    consumer.subscribe(
        [f"{name}_land"], on_assign=lambda _c, partitions: assigned.extend(partitions)
    )
    deadline = time.monotonic() + timeout
    while not assigned:
        assert time.monotonic() < deadline, "the transform's group never assigned it a partition"
        consumer.poll(0.5)
    _commit(consumer, [TopicPartition(tp.topic, tp.partition, 0) for tp in assigned])
    return consumer


def _transform_group(name: str) -> str:
    """The group dfe-transform-vector derives from its ``dfe_source`` (its config/loader.rs)."""
    return f"dfe-transform-vector-{name}"


def _group_state(bootstrap: str, group: str) -> tuple[str, int]:
    admin = AdminClient({"bootstrap.servers": bootstrap})
    described = admin.describe_consumer_groups([group], request_timeout=10)[group].result()
    return described.state.name.lower(), len(described.members)


def _transform_runs(stack: SimpleNamespace, name: str) -> bool:
    return instances.exists(stack.crud, instances.instance_of(TRANSFORM_APP, name))


def _deployed_with_a_transform(stack: SimpleNamespace, bootstrap: str) -> str:
    """Create and deploy a transforming source, fill its ``_load`` topic, and drop the transform."""
    name = f"drops-{uuid.uuid4().hex[:8]}"
    created = stack.client.post(
        "/api/v1/sources", headers=stack.headers, json=_body(name, transform=True)
    )
    assert created.status_code == 201, created.text
    first = stack.client.post(f"/api/v1/sources/{name}/deploy", headers=stack.headers)
    assert first.status_code == 200, first.text
    assert {f"{name}_land", f"{name}_load"} <= set(first.json()["topics_ensured"])
    assert _transform_runs(stack, name), first.json()["apps_sync_error"]
    _produce(bootstrap, f"{name}_load")
    updated = stack.client.put(
        f"/api/v1/sources/{name}", headers=stack.headers, json=_body(name, transform=False)
    )
    assert updated.status_code == 200, updated.text
    return name


def _deploy_without_it(stack: SimpleNamespace, bootstrap: str, name: str) -> None:
    """Deploy the version without the transform, which goes ahead with ``_load`` pending."""
    deployed = stack.client.post(f"/api/v1/sources/{name}/deploy", headers=stack.headers)

    assert deployed.status_code == 200, deployed.text
    body = deployed.json()
    assert (body["topics_removed"], body["topics_pending_removal"]) == ([], [f"{name}_load"])
    assert f"{name}_load" in _topics(bootstrap)
    assert not _transform_runs(stack, name)
    source = stack.client.get(f"/api/v1/sources/{name}", headers=stack.headers).json()
    assert source["deployed_version"] == source["current"]
    assert source["transform"] is None


def _reconcile(stack: SimpleNamespace) -> dict:
    reconciled = stack.client.post("/api/v1/sources/reconcile-apps", headers=stack.headers)
    assert reconciled.status_code == 200, reconciled.text
    return reconciled.json()


def test_a_live_backlog_never_stops_the_deploy_and_a_later_pass_removes_the_drained_topic(
    kafka_stack, kafka_bootstrap
):
    name = _deployed_with_a_transform(kafka_stack, kafka_bootstrap)
    _loader_reads_to(kafka_bootstrap, f"{name}_load", 1)

    _deploy_without_it(kafka_stack, kafka_bootstrap, name)

    # The instance has gone, but the loader has records left to read.
    held = _reconcile(kafka_stack)
    assert (held["topics_removed"], held["topics_pending_removal"]) == ([], [f"{name}_load"])
    assert f"{name}_load" in _topics(kafka_bootstrap)

    _loader_reads_to(kafka_bootstrap, f"{name}_load", RECORDS)
    cleared = _reconcile(kafka_stack)

    assert (cleared["topics_removed"], cleared["topics_pending_removal"]) == ([f"{name}_load"], [])
    topics = _topics(kafka_bootstrap)
    assert f"{name}_load" not in topics
    assert f"{name}_land" in topics


def test_a_drained_topic_waits_for_the_pass_after_its_instance_leaves(kafka_stack, kafka_bootstrap):
    name = _deployed_with_a_transform(kafka_stack, kafka_bootstrap)
    _loader_reads_to(kafka_bootstrap, f"{name}_load", RECORDS)

    _deploy_without_it(kafka_stack, kafka_bootstrap, name)
    cleared = _reconcile(kafka_stack)

    assert (cleared["topics_removed"], cleared["topics_pending_removal"]) == ([f"{name}_load"], [])
    assert f"{name}_load" not in _topics(kafka_bootstrap)


def test_a_live_member_in_the_transforms_group_holds_the_drained_topic(
    kafka_stack, kafka_bootstrap
):
    # Its overlay is gone, but the pod runs on until Argo syncs and it drains.
    name = _deployed_with_a_transform(kafka_stack, kafka_bootstrap)
    transform = _transform_joins(kafka_bootstrap, name)
    try:
        _loader_reads_to(kafka_bootstrap, f"{name}_load", RECORDS)
        _deploy_without_it(kafka_stack, kafka_bootstrap, name)
        assert _group_state(kafka_bootstrap, _transform_group(name)) == ("stable", 1)

        held = _reconcile(kafka_stack)

        assert (held["topics_removed"], held["topics_pending_removal"]) == ([], [f"{name}_load"])
        assert f"{name}_load" in _topics(kafka_bootstrap)
    finally:
        transform.close()

    cleared = _reconcile(kafka_stack)
    assert cleared["topics_removed"] == [f"{name}_load"]


def test_an_empty_transform_group_and_no_backlog_let_the_next_pass_delete(
    kafka_stack, kafka_bootstrap
):
    name = _deployed_with_a_transform(kafka_stack, kafka_bootstrap)
    _transform_joins(kafka_bootstrap, name).close()
    assert _group_state(kafka_bootstrap, _transform_group(name)) == ("empty", 0)
    _loader_reads_to(kafka_bootstrap, f"{name}_load", RECORDS)
    _deploy_without_it(kafka_stack, kafka_bootstrap, name)

    cleared = _reconcile(kafka_stack)

    assert (cleared["topics_removed"], cleared["topics_pending_removal"]) == ([f"{name}_load"], [])
    assert f"{name}_load" not in _topics(kafka_bootstrap)


def test_a_load_topic_no_transform_of_the_source_wrote_is_held(kafka_stack, kafka_bootstrap):
    # No version names a transform, so there is no consumer group to say its writer is gone.
    name = f"drops-{uuid.uuid4().hex[:8]}"
    created = kafka_stack.client.post(
        "/api/v1/sources", headers=kafka_stack.headers, json=_body(name, transform=False)
    )
    assert created.status_code == 201, created.text
    first = kafka_stack.client.post(f"/api/v1/sources/{name}/deploy", headers=kafka_stack.headers)
    assert first.status_code == 200, first.text
    admin = AdminClient({"bootstrap.servers": kafka_bootstrap})
    load = f"{name}_load"
    admin.create_topics([NewTopic(load, num_partitions=1, replication_factor=1)])[load].result()

    again = kafka_stack.client.post(f"/api/v1/sources/{name}/deploy", headers=kafka_stack.headers)

    assert again.status_code == 200, again.text
    assert (again.json()["topics_removed"], again.json()["topics_pending_removal"]) == ([], [load])
    _reconcile(kafka_stack)
    assert load in _topics(kafka_bootstrap)


def test_a_restart_deletes_a_topic_left_pending(kafka_engine_restarts, kafka_bootstrap):
    with kafka_engine_restarts() as first:
        name = _deployed_with_a_transform(first, kafka_bootstrap)
        _transform_joins(kafka_bootstrap, name).close()
        _loader_reads_to(kafka_bootstrap, f"{name}_load", RECORDS)
        _deploy_without_it(first, kafka_bootstrap, name)

    with kafka_engine_restarts():
        topics = _topics(kafka_bootstrap)

    assert f"{name}_load" not in topics
    assert f"{name}_land" in topics
