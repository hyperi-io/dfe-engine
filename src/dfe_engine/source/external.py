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
and :func:`header_fields` says what each one costs when it is missing. It holds
``_org_id`` and ``_source`` on the pipeline's topic to the source's own org and
name plus the source's allow-lists, so a pipeline cannot write into another
tenant's data or another source's table.

See docs/data-plane/external-pipelines.md.
"""

import functools
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from dfe_engine.kafka.topics import TopicAdmin, consumer_group_backlog
from dfe_engine.schema.schema_loader import SchemaLoader
from dfe_engine.source.models import (
    DEFAULT_HEADER_TYPE,
    Source,
    external_consumer_group,
    transformed_topic,
)

if TYPE_CHECKING:
    from dfe_engine.settings import DFESettings

ExternalRole = Literal["origin", "transform"]


@dataclass(frozen=True, slots=True)
class HeaderField:
    """One common header field, and what setting it buys over the loader's default."""

    field: str
    when_missing: str
    when_set: str


# What a value the pipeline sets buys. The header declares where a field is read
# from and not why that is worth doing, so this is the one piece of prose kept here.
_WHEN_SET: dict[str, str] = {
    "_timestamp": "the event's own time, so time-range queries and retention follow when it happened",
    "_org_id": (
        "the source's org, or one allowed_org_ids lists, is kept, which is how a provider "
        "lands each customer's records in that customer's org. Any other value is "
        "replaced with the source's org and counted"
    ),
    "_source": (
        "this source, or one allowed_sources lists, is kept and picks the table. Any other "
        "value is replaced with this source and counted"
    ),
    "_tags": "tags a query or an alert can filter on without parsing the payload",
    "_timestamp_received": (
        "when the pipeline first saw the event, so the delay before DFE can be measured"
    ),
}

# The fallbacks the header's expressions name, in the words a customer reads.
_FALLBACKS: dict[str, str] = {
    "now()": "it takes the load time",
    "topic_name": "it takes the topic name, which is this source",
}

# What the loader does when none of a field's candidates is there and the
# expression names no fallback of its own. The loader owns these, not the header.
_WHEN_NONE: dict[str, str] = {
    "_org_id": "the record lands in the source's org",
    "_tags": "it stays empty",
    "_timestamp_received": "it takes the load time",
}

_SOURCE_EXPR = re.compile(
    r"^@source:\s*(?:first\((?P<first>[^)]+)\)|(?P<field>[^|\s][^|]*?))\s*(?:\|\s*(?P<fallback>.+?))?\s*$"
)


def reads_from_expr(field: str, expr: str) -> str | None:
    """What the loader does with a header column's ``@source`` expression, in words.

    ``@source: timestamp | now()`` reads "the loader reads timestamp, and when it is
    not there it takes the load time". An expression that is not an ``@source``
    returns None, because the loader writes that column itself.
    """
    match = _SOURCE_EXPR.match(expr.strip())
    if match is None:
        return None
    first = match.group("first")
    candidates = (
        [c.strip() for c in first.split("/") if c.strip()]
        if first
        else [match.group("field").strip()]
    )
    text = "the loader reads " + ", then ".join(candidates)
    fallback = match.group("fallback")
    outcome = _FALLBACKS.get(fallback.strip(), f"it uses {fallback.strip()}") if fallback else None
    outcome = outcome or _WHEN_NONE.get(field)
    if outcome:
        absent = "it is not there" if len(candidates) == 1 else "none is there"
        text += f", and when {absent} {outcome}"
    return text


@functools.cache
def header_fields(
    header_type: str = DEFAULT_HEADER_TYPE, header_version: str | None = None
) -> tuple[HeaderField, ...]:
    """The fields the header profile reads from a record, in its column order.

    Read from the pinned dfe-schemas profile at its current version unless one is given, so the read order named here is the
    one the loader applies. ``_timestamp_load``, ``_uuid``, ``_json`` and ``_raw``
    carry no ``@source`` expression: the loader and ClickHouse always write those,
    and a value the pipeline sets is not read.
    """
    fields = []
    for column in SchemaLoader.load_profile(header_type, profile_version=header_version):
        reads = reads_from_expr(column.name, column.expr or "")
        if reads is None:
            continue
        fields.append(
            HeaderField(
                field=column.name,
                when_missing=reads,
                when_set=_WHEN_SET.get(column.name, "the pipeline's own value"),
            )
        )
    return tuple(fields)


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
    """A record carrying every field in :func:`header_fields`, for the pipeline to copy."""
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
