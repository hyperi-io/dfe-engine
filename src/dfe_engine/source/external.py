#  Project:      dfe-engine
#  File:         source/external.py
#  Purpose:      What a customer's own pipeline needs to plug into a source on the bus
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The connection a customer's Vector, Fluentd or Logstash takes to one source.

A pipeline the customer already runs plugs in on the bus in one of two places:
as the source's ORIGIN, producing to ``<source>_land`` past the receiver, or as
its TRANSFORM, reading ``<source>_land`` in :func:`external_consumer_group` and
producing to ``<source>_load``. The engine creates both topics and deploys
nothing for the pipeline. The credential is the customer's own, and the engine
keeps only the reference the operator gives it.

The loader fills in a missing common header field as it does for any producer,
and :data:`HEADER_FIELDS` says what each one costs when it is missing. It holds
``_org_id`` and ``_source`` on the pipeline's topic to the source's own org and
name plus the source's allow-lists, so a pipeline cannot write into another
tenant's data or another source's table.

See docs/data-plane/external-pipelines.md.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from dfe_engine.kafka.topics import TopicAdmin, consumer_group_backlog
from dfe_engine.source.models import Source, external_consumer_group, transformed_topic

if TYPE_CHECKING:
    from dfe_engine.settings import DFESettings

ExternalRole = Literal["origin", "transform"]


@dataclass(frozen=True, slots=True)
class HeaderField:
    """One common header field, and what setting it buys over the loader's default."""

    field: str
    when_missing: str
    when_set: str


HEADER_FIELDS: tuple[HeaderField, ...] = (
    HeaderField(
        "_timestamp",
        "the loader reads timestamp, then @timestamp, and takes the load time when "
        "neither is there",
        "the event's own time, so time-range queries and retention follow when it happened",
    ),
    HeaderField(
        "_org_id",
        "the record lands in the source's org",
        "the source's org, or one allowed_org_ids lists, is kept, which is how a provider "
        "lands each customer's records in that customer's org. Any other value is "
        "replaced with the source's org and counted",
    ),
    HeaderField(
        "_source",
        "the loader takes it from the topic name, which is this source",
        "this source, or one allowed_sources lists, is kept and picks the table. Any other "
        "value is replaced with this source and counted",
    ),
    HeaderField(
        "_tags",
        "the loader reads tags, then meta, then metadata.tags, and leaves it empty "
        "when none is there",
        "tags a query or an alert can filter on without parsing the payload",
    ),
    HeaderField(
        "_timestamp_received",
        "the loader takes the load time",
        "when the pipeline first saw the event, so the delay before DFE can be measured",
    ),
)
"""The fields the default header profile reads from a record, in dfe-schemas column order.

``_timestamp_load``, ``_uuid``, ``_json`` and ``_raw`` are left out: the loader
and ClickHouse always write those, and a value the pipeline sets is not read.
"""


@dataclass(frozen=True, slots=True)
class ExternalConnection:
    """What one customer pipeline is told to connect with."""

    role: ExternalRole
    reads: str | None
    """The topic a transform consumes. None for an origin, which reads nothing of ours."""

    writes: str
    group: str | None
    """The consumer group a transform reads in. Fixed, because the engine reads its lag."""

    bootstrap: str
    security_protocol: str
    sasl_mechanism: str
    credential_ref: str | None
    org: str | None
    """The registered org the records belong to, None for the default org."""

    allowed_org_ids: tuple[str, ...]
    allowed_sources: tuple[str, ...]


def external_connection(
    source: Source, settings: DFESettings, version_id: str | None = None
) -> ExternalConnection | None:
    """The connection *source*'s external pipeline takes, or None when it has none.

    The bootstrap is the address a pipeline outside the deployment reaches the
    broker on, and the security protocol and mechanism are the ones the broker
    contract derives for this deployment's provider.
    """
    pipeline = source.external_pipeline(version_id)
    if pipeline is None:
        return None
    kafka = settings.kafka
    bootstrap = kafka.external_bootstrap_servers or kafka.bootstrap_servers
    if source.version(version_id).external is not None:
        role: ExternalRole = "origin"
        reads, writes, group = None, source.topic_land, None
    else:
        role = "transform"
        reads, writes = source.topic_land, transformed_topic(source.source)
        group = external_consumer_group(source.source)
    return ExternalConnection(
        role=role,
        reads=reads,
        writes=writes,
        group=group,
        bootstrap=bootstrap,
        security_protocol=kafka.security_protocol,
        sasl_mechanism=kafka.sasl_mechanism,
        credential_ref=pipeline.credential_ref,
        org=pipeline.org,
        allowed_org_ids=tuple(pipeline.allowed_org_ids),
        allowed_sources=tuple(pipeline.allowed_sources),
    )


def sample_record(source: Source) -> dict[str, object]:
    """A record carrying every field in :data:`HEADER_FIELDS`, for the pipeline to copy."""
    return {
        "_timestamp": "2026-11-03T04:05:06.789Z",
        "_org_id": "<your org's tenant id>",
        "_source": source.source,
        "_tags": {"env": "prod"},
        "_timestamp_received": "2026-11-03T04:05:06.123Z",
        "message": "the event as your pipeline produced it",
    }


@dataclass(frozen=True, slots=True)
class GroupLag:
    """The external transform's consumer group on the landing topic, as the broker reports it.

    ``committed`` is False while the group has never committed an offset there,
    so every record on the topic counts as unread.
    """

    group: str
    topic: str
    lag: int
    committed: bool
    state: str
    members: int


def external_group_lag(source: Source, admin: TopicAdmin) -> GroupLag:
    """How far the customer's transform is behind on *source*'s landing topic.

    Raises:
        Exception: The broker did not answer one of the reads.
    """
    group = external_consumer_group(source.source)
    topic = source.topic_land
    lag, committed = consumer_group_backlog(topic, group, admin=admin)
    state, members = admin.describe_group(group)
    return GroupLag(
        group=group, topic=topic, lag=lag, committed=committed, state=state, members=members
    )
