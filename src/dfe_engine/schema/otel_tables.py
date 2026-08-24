#  Project:      dfe-engine
#  File:         schema/otel_tables.py
#  Purpose:      The OTel telemetry tables, owned by DFE rather than the exporter
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The OTel telemetry tables: logs, traces and the five metric shapes.

Seeded from the HyperDX collector preset the DFE stack deploys -- ``otel_logs``
carries its ``__otel_materialized_k8s.*`` columns and text indexes, not the
plainer upstream default. Held here so they can be optimised like any other DFE
table rather than being whatever the exporter happened to create.

Owning them is also what makes them correct on a cluster. The exporter creates
its tables over whichever connection the Service resolved and senses no
topology, so the same table ended up plain on one replica and Replicated on the
others: rows written to the plain copy never replicate, every query succeeds,
and the answer depends which replica it landed on. Through
:class:`~dfe_engine.schema.applier.SchemaApplier` the engine clause is resolved
once and applied ON CLUSTER. The exporter is configured with
``create_schema: false`` so it only ever INSERTs.
"""

from __future__ import annotations

from dataclasses import replace

from scalo.logger import logger

from dfe_engine.schema.schema_ddl import DDLConfig, TableSpec
from dfe_engine.schema.schema_loader import SchemaLoader
from dfe_engine.source.models import SchemaColumn

_LOW_CARDINALITY = "LowCardinality("

TRACE_ID_TS_TABLE = "otel_traces_trace_id_ts"
TRACE_ID_TS_VIEW = f"{TRACE_ID_TS_TABLE}_mv"

LOAD_TIMESTAMP = "_timestamp_load"
"""The common-header column every otel table takes, matching every DFE table."""

LOGS_TABLE = "otel_logs"
BODY_JSON = "BodyJson"

_FALLBACK_JSON_MAX_PATHS = 2048
"""Used only when the common header cannot be read; see ``_body_json_max_paths``."""


def _col(
    name: str,
    ch_type: str,
    *,
    codec: str | None = None,
    comment: str | None = None,
    materialized: str | None = None,
    default: str | None = None,
    max_dynamic_paths: int | None = None,
) -> SchemaColumn:
    """One OTel column, typed by exact ClickHouse type.

    A top-level ``LowCardinality(...)`` wrapper becomes an attribute: the
    override catalogue takes the inner type, not the wrapped form. A nested one
    (``Map(LowCardinality(String), String)``) is part of the type and stays.
    """
    attribute: list[str] = []
    if ch_type.startswith(_LOW_CARDINALITY) and ch_type.endswith(")"):
        ch_type = ch_type[len(_LOW_CARDINALITY) : -1]
        attribute.append("lowcardinality")
    if materialized:
        attribute.append("materialized")
        default = materialized
    return SchemaColumn(
        name=name,
        type="string",
        ch_override=ch_type,
        attribute=attribute,
        codec=codec,
        comment=comment,
        default=default,
        max_dynamic_paths=max_dynamic_paths,
    )


def _otel_config(
    database: str,
    *,
    engine: str,
    partition_by: str,
    order_by: str,
    indexes: list[str] | None = None,
) -> DDLConfig:
    """DDL config for an OTel table.

    Every clause is raw: these order by expressions rather than bare columns and
    index over map projections, neither of which the column model expresses. No
    TTL and no projection -- retention is the deployment's call, and the
    exporter's tables never carried either.
    """
    return DDLConfig(
        db=database,
        engine=engine,
        ttl_days=None,
        ttl_columns=[],
        projection_order_by=None,
        partition_by=partition_by,
        order_by=order_by,
        extra_indexes=list(indexes or []),
        # The exporter's own value, and ClickHouse's default. The DFE data
        # tables run a finer 2048 for point lookups; these are scanned in ranges.
        index_granularity=8192,
    )


def otel_logs_spec(database: str) -> TableSpec:
    """The exporter's ``otel_logs`` table."""
    return TableSpec(
        name="otel_logs",
        columns=[
            _col(
                "Timestamp",
                "DateTime64(9)",
                codec="Delta(8), ZSTD(1)",
                comment="Event timestamp with nanosecond precision",
            ),
            _col("TraceId", "String", codec="ZSTD(1)", comment="W3C trace identifier"),
            _col("SpanId", "String", codec="ZSTD(1)", comment="W3C span identifier"),
            _col("TraceFlags", "UInt8", comment="W3C trace flags"),
            _col(
                "SeverityText",
                "LowCardinality(String)",
                codec="ZSTD(1)",
                comment="Log severity as text",
            ),
            _col("SeverityNumber", "UInt8", comment="Log severity as number (1-24)"),
            _col(
                "ServiceName",
                "LowCardinality(String)",
                codec="ZSTD(1)",
                comment="Service that emitted the log",
            ),
            _col("Body", "String", codec="ZSTD(1)", comment="Log message body"),
            _col(
                "ResourceSchemaUrl",
                "LowCardinality(String)",
                codec="ZSTD(1)",
                comment="Schema URL for the resource",
            ),
            _col(
                "ResourceAttributes",
                "Map(LowCardinality(String), String)",
                codec="ZSTD(1)",
                comment="Resource attributes as key-value pairs",
            ),
            _col(
                "ScopeSchemaUrl",
                "LowCardinality(String)",
                codec="ZSTD(1)",
                comment="Schema URL for the instrumentation scope",
            ),
            _col("ScopeName", "String", codec="ZSTD(1)", comment="Instrumentation scope name"),
            _col(
                "ScopeVersion",
                "LowCardinality(String)",
                codec="ZSTD(1)",
                comment="Instrumentation scope version",
            ),
            _col(
                "ScopeAttributes",
                "Map(LowCardinality(String), String)",
                codec="ZSTD(1)",
                comment="Instrumentation scope attributes",
            ),
            _col(
                "LogAttributes",
                "Map(LowCardinality(String), String)",
                codec="ZSTD(1)",
                comment="Log record attributes",
            ),
            _col(
                "EventName",
                "String",
                codec="ZSTD(1)",
                comment="Event name for log records representing events",
            ),
            _col(
                "__otel_materialized_k8s.cluster.name",
                "LowCardinality(String)",
                codec="ZSTD(1)",
                materialized="ResourceAttributes['k8s.cluster.name']",
            ),
            _col(
                "__otel_materialized_k8s.container.name",
                "LowCardinality(String)",
                codec="ZSTD(1)",
                materialized="ResourceAttributes['k8s.container.name']",
            ),
            _col(
                "__otel_materialized_k8s.deployment.name",
                "LowCardinality(String)",
                codec="ZSTD(1)",
                materialized="ResourceAttributes['k8s.deployment.name']",
            ),
            _col(
                "__otel_materialized_k8s.namespace.name",
                "LowCardinality(String)",
                codec="ZSTD(1)",
                materialized="ResourceAttributes['k8s.namespace.name']",
            ),
            _col(
                "__otel_materialized_k8s.node.name",
                "LowCardinality(String)",
                codec="ZSTD(1)",
                materialized="ResourceAttributes['k8s.node.name']",
            ),
            _col(
                "__otel_materialized_k8s.pod.name",
                "LowCardinality(String)",
                codec="ZSTD(1)",
                materialized="ResourceAttributes['k8s.pod.name']",
            ),
            _col(
                "__otel_materialized_k8s.pod.uid",
                "LowCardinality(String)",
                codec="ZSTD(1)",
                materialized="ResourceAttributes['k8s.pod.uid']",
            ),
            _col(
                "__otel_materialized_deployment.environment.name",
                "LowCardinality(String)",
                codec="ZSTD(1)",
                materialized="ResourceAttributes['deployment.environment.name']",
            ),
        ],
        config=_otel_config(
            database,
            engine="MergeTree",
            partition_by="toDate(Timestamp)",
            order_by="toStartOfFiveMinutes(Timestamp), ServiceName, Timestamp",
            indexes=[
                "INDEX idx_trace_id TraceId TYPE text(tokenizer = 'array') GRANULARITY 100000000",
                "INDEX idx_res_attr_key mapKeys(ResourceAttributes) TYPE text(tokenizer = 'array') GRANULARITY 100000000",
                "INDEX idx_res_attr_value mapValues(ResourceAttributes) TYPE text(tokenizer = 'array') GRANULARITY 100000000",
                "INDEX idx_scope_attr_key mapKeys(ScopeAttributes) TYPE text(tokenizer = 'array') GRANULARITY 100000000",
                "INDEX idx_scope_attr_value mapValues(ScopeAttributes) TYPE text(tokenizer = 'array') GRANULARITY 100000000",
                "INDEX idx_log_attr_key mapKeys(LogAttributes) TYPE text(tokenizer = 'array') GRANULARITY 100000000",
                "INDEX idx_log_attr_value mapValues(LogAttributes) TYPE text(tokenizer = 'array') GRANULARITY 100000000",
                "INDEX idx_lower_body lower(Body) TYPE text(tokenizer = 'splitByNonAlpha') GRANULARITY 100000000",
            ],
        ),
    )


def otel_metrics_exponential_histogram_spec(database: str) -> TableSpec:
    """The exporter's ``otel_metrics_exponential_histogram`` table."""
    return TableSpec(
        name="otel_metrics_exponential_histogram",
        columns=[
            _col("ResourceAttributes", "Map(LowCardinality(String), String)", codec="ZSTD(1)"),
            _col("ResourceSchemaUrl", "String", codec="ZSTD(1)"),
            _col("ScopeName", "String", codec="ZSTD(1)"),
            _col("ScopeVersion", "String", codec="ZSTD(1)"),
            _col("ScopeAttributes", "Map(LowCardinality(String), String)", codec="ZSTD(1)"),
            _col("ScopeDroppedAttrCount", "UInt32", codec="ZSTD(1)"),
            _col("ScopeSchemaUrl", "String", codec="ZSTD(1)"),
            _col("ServiceName", "LowCardinality(String)", codec="ZSTD(1)"),
            _col("MetricName", "LowCardinality(String)", codec="ZSTD(1)"),
            _col("MetricDescription", "String", codec="ZSTD(1)"),
            _col("MetricUnit", "String", codec="ZSTD(1)"),
            _col("Attributes", "Map(LowCardinality(String), String)", codec="ZSTD(1)"),
            _col("StartTimeUnix", "DateTime", codec="Delta(4), ZSTD(1)"),
            _col("TimeUnix", "DateTime", codec="Delta(4), ZSTD(1)"),
            _col("Count", "UInt64", codec="Delta(8), ZSTD(1)"),
            _col("Sum", "Float64", codec="ZSTD(1)"),
            _col("Scale", "Int32", codec="ZSTD(1)"),
            _col("ZeroCount", "UInt64", codec="ZSTD(1)"),
            _col("PositiveOffset", "Int32", codec="ZSTD(1)"),
            _col("PositiveBucketCounts", "Array(UInt64)", codec="ZSTD(1)"),
            _col("NegativeOffset", "Int32", codec="ZSTD(1)"),
            _col("NegativeBucketCounts", "Array(UInt64)", codec="ZSTD(1)"),
            _col(
                "Exemplars.FilteredAttributes",
                "Array(Map(LowCardinality(String), String))",
                codec="ZSTD(1)",
            ),
            _col("Exemplars.TimeUnix", "Array(DateTime)", codec="ZSTD(1)"),
            _col("Exemplars.Value", "Array(Float64)", codec="ZSTD(1)"),
            _col("Exemplars.SpanId", "Array(String)", codec="ZSTD(1)"),
            _col("Exemplars.TraceId", "Array(String)", codec="ZSTD(1)"),
            _col("Flags", "UInt32", codec="ZSTD(1)"),
            _col("Min", "Float64", codec="ZSTD(1)"),
            _col("Max", "Float64", codec="ZSTD(1)"),
            _col("AggregationTemporality", "Int32", codec="ZSTD(1)"),
        ],
        config=_otel_config(
            database,
            engine="MergeTree",
            partition_by="toDate(TimeUnix)",
            order_by="ServiceName, MetricName, toStartOfHour(TimeUnix), cityHash64(Attributes), TimeUnix",
            indexes=[
                "INDEX idx_res_attr_key mapKeys(ResourceAttributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_res_attr_value mapValues(ResourceAttributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_scope_attr_key mapKeys(ScopeAttributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_scope_attr_value mapValues(ScopeAttributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_attr_key mapKeys(Attributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_attr_value mapValues(Attributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_time_minmax TimeUnix TYPE minmax GRANULARITY 1",
            ],
        ),
    )


def otel_metrics_gauge_spec(database: str) -> TableSpec:
    """The exporter's ``otel_metrics_gauge`` table."""
    return TableSpec(
        name="otel_metrics_gauge",
        columns=[
            _col("ResourceAttributes", "Map(LowCardinality(String), String)", codec="ZSTD(1)"),
            _col("ResourceSchemaUrl", "String", codec="ZSTD(1)"),
            _col("ScopeName", "String", codec="ZSTD(1)"),
            _col("ScopeVersion", "String", codec="ZSTD(1)"),
            _col("ScopeAttributes", "Map(LowCardinality(String), String)", codec="ZSTD(1)"),
            _col("ScopeDroppedAttrCount", "UInt32", codec="ZSTD(1)"),
            _col("ScopeSchemaUrl", "String", codec="ZSTD(1)"),
            _col("ServiceName", "LowCardinality(String)", codec="ZSTD(1)"),
            _col("MetricName", "LowCardinality(String)", codec="ZSTD(1)"),
            _col("MetricDescription", "String", codec="ZSTD(1)"),
            _col("MetricUnit", "String", codec="ZSTD(1)"),
            _col("Attributes", "Map(LowCardinality(String), String)", codec="ZSTD(1)"),
            _col("StartTimeUnix", "DateTime", codec="Delta(4), ZSTD(1)"),
            _col("TimeUnix", "DateTime", codec="Delta(4), ZSTD(1)"),
            _col("Value", "Float64", codec="ZSTD(1)"),
            _col("Flags", "UInt32", codec="ZSTD(1)"),
            _col(
                "Exemplars.FilteredAttributes",
                "Array(Map(LowCardinality(String), String))",
                codec="ZSTD(1)",
            ),
            _col("Exemplars.TimeUnix", "Array(DateTime)", codec="ZSTD(1)"),
            _col("Exemplars.Value", "Array(Float64)", codec="ZSTD(1)"),
            _col("Exemplars.SpanId", "Array(String)", codec="ZSTD(1)"),
            _col("Exemplars.TraceId", "Array(String)", codec="ZSTD(1)"),
        ],
        config=_otel_config(
            database,
            engine="MergeTree",
            partition_by="toDate(TimeUnix)",
            order_by="ServiceName, MetricName, toStartOfHour(TimeUnix), cityHash64(Attributes), TimeUnix",
            indexes=[
                "INDEX idx_res_attr_key mapKeys(ResourceAttributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_res_attr_value mapValues(ResourceAttributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_scope_attr_key mapKeys(ScopeAttributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_scope_attr_value mapValues(ScopeAttributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_attr_key mapKeys(Attributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_attr_value mapValues(Attributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_time_minmax TimeUnix TYPE minmax GRANULARITY 1",
            ],
        ),
    )


def otel_metrics_histogram_spec(database: str) -> TableSpec:
    """The exporter's ``otel_metrics_histogram`` table."""
    return TableSpec(
        name="otel_metrics_histogram",
        columns=[
            _col("ResourceAttributes", "Map(LowCardinality(String), String)", codec="ZSTD(1)"),
            _col("ResourceSchemaUrl", "String", codec="ZSTD(1)"),
            _col("ScopeName", "String", codec="ZSTD(1)"),
            _col("ScopeVersion", "String", codec="ZSTD(1)"),
            _col("ScopeAttributes", "Map(LowCardinality(String), String)", codec="ZSTD(1)"),
            _col("ScopeDroppedAttrCount", "UInt32", codec="ZSTD(1)"),
            _col("ScopeSchemaUrl", "String", codec="ZSTD(1)"),
            _col("ServiceName", "LowCardinality(String)", codec="ZSTD(1)"),
            _col("MetricName", "LowCardinality(String)", codec="ZSTD(1)"),
            _col("MetricDescription", "String", codec="ZSTD(1)"),
            _col("MetricUnit", "String", codec="ZSTD(1)"),
            _col("Attributes", "Map(LowCardinality(String), String)", codec="ZSTD(1)"),
            _col("StartTimeUnix", "DateTime", codec="Delta(4), ZSTD(1)"),
            _col("TimeUnix", "DateTime", codec="Delta(4), ZSTD(1)"),
            _col("Count", "UInt64", codec="Delta(8), ZSTD(1)"),
            _col("Sum", "Float64", codec="ZSTD(1)"),
            _col("BucketCounts", "Array(UInt64)", codec="ZSTD(1)"),
            _col("ExplicitBounds", "Array(Float64)", codec="ZSTD(1)"),
            _col(
                "Exemplars.FilteredAttributes",
                "Array(Map(LowCardinality(String), String))",
                codec="ZSTD(1)",
            ),
            _col("Exemplars.TimeUnix", "Array(DateTime)", codec="ZSTD(1)"),
            _col("Exemplars.Value", "Array(Float64)", codec="ZSTD(1)"),
            _col("Exemplars.SpanId", "Array(String)", codec="ZSTD(1)"),
            _col("Exemplars.TraceId", "Array(String)", codec="ZSTD(1)"),
            _col("Flags", "UInt32", codec="ZSTD(1)"),
            _col("Min", "Float64", codec="ZSTD(1)"),
            _col("Max", "Float64", codec="ZSTD(1)"),
            _col("AggregationTemporality", "Int32", codec="ZSTD(1)"),
        ],
        config=_otel_config(
            database,
            engine="MergeTree",
            partition_by="toDate(TimeUnix)",
            order_by="ServiceName, MetricName, toStartOfHour(TimeUnix), cityHash64(Attributes), TimeUnix",
            indexes=[
                "INDEX idx_res_attr_key mapKeys(ResourceAttributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_res_attr_value mapValues(ResourceAttributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_scope_attr_key mapKeys(ScopeAttributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_scope_attr_value mapValues(ScopeAttributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_attr_key mapKeys(Attributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_attr_value mapValues(Attributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_time_minmax TimeUnix TYPE minmax GRANULARITY 1",
            ],
        ),
    )


def otel_metrics_sum_spec(database: str) -> TableSpec:
    """The exporter's ``otel_metrics_sum`` table."""
    return TableSpec(
        name="otel_metrics_sum",
        columns=[
            _col("ResourceAttributes", "Map(LowCardinality(String), String)", codec="ZSTD(1)"),
            _col("ResourceSchemaUrl", "String", codec="ZSTD(1)"),
            _col("ScopeName", "String", codec="ZSTD(1)"),
            _col("ScopeVersion", "String", codec="ZSTD(1)"),
            _col("ScopeAttributes", "Map(LowCardinality(String), String)", codec="ZSTD(1)"),
            _col("ScopeDroppedAttrCount", "UInt32", codec="ZSTD(1)"),
            _col("ScopeSchemaUrl", "String", codec="ZSTD(1)"),
            _col("ServiceName", "LowCardinality(String)", codec="ZSTD(1)"),
            _col("MetricName", "LowCardinality(String)", codec="ZSTD(1)"),
            _col("MetricDescription", "String", codec="ZSTD(1)"),
            _col("MetricUnit", "String", codec="ZSTD(1)"),
            _col("Attributes", "Map(LowCardinality(String), String)", codec="ZSTD(1)"),
            _col("StartTimeUnix", "DateTime", codec="Delta(4), ZSTD(1)"),
            _col("TimeUnix", "DateTime", codec="Delta(4), ZSTD(1)"),
            _col("Value", "Float64", codec="ZSTD(1)"),
            _col("Flags", "UInt32", codec="ZSTD(1)"),
            _col(
                "Exemplars.FilteredAttributes",
                "Array(Map(LowCardinality(String), String))",
                codec="ZSTD(1)",
            ),
            _col("Exemplars.TimeUnix", "Array(DateTime)", codec="ZSTD(1)"),
            _col("Exemplars.Value", "Array(Float64)", codec="ZSTD(1)"),
            _col("Exemplars.SpanId", "Array(String)", codec="ZSTD(1)"),
            _col("Exemplars.TraceId", "Array(String)", codec="ZSTD(1)"),
            _col("AggregationTemporality", "Int32", codec="ZSTD(1)"),
            _col("IsMonotonic", "Bool", codec="Delta(1), ZSTD(1)"),
        ],
        config=_otel_config(
            database,
            engine="MergeTree",
            partition_by="toDate(TimeUnix)",
            order_by="ServiceName, MetricName, toStartOfHour(TimeUnix), cityHash64(Attributes), TimeUnix",
            indexes=[
                "INDEX idx_res_attr_key mapKeys(ResourceAttributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_res_attr_value mapValues(ResourceAttributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_scope_attr_key mapKeys(ScopeAttributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_scope_attr_value mapValues(ScopeAttributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_attr_key mapKeys(Attributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_attr_value mapValues(Attributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_time_minmax TimeUnix TYPE minmax GRANULARITY 1",
            ],
        ),
    )


def otel_metrics_summary_spec(database: str) -> TableSpec:
    """The exporter's ``otel_metrics_summary`` table."""
    return TableSpec(
        name="otel_metrics_summary",
        columns=[
            _col("ResourceAttributes", "Map(LowCardinality(String), String)", codec="ZSTD(1)"),
            _col("ResourceSchemaUrl", "String", codec="ZSTD(1)"),
            _col("ScopeName", "String", codec="ZSTD(1)"),
            _col("ScopeVersion", "String", codec="ZSTD(1)"),
            _col("ScopeAttributes", "Map(LowCardinality(String), String)", codec="ZSTD(1)"),
            _col("ScopeDroppedAttrCount", "UInt32", codec="ZSTD(1)"),
            _col("ScopeSchemaUrl", "String", codec="ZSTD(1)"),
            _col("ServiceName", "LowCardinality(String)", codec="ZSTD(1)"),
            _col("MetricName", "LowCardinality(String)", codec="ZSTD(1)"),
            _col("MetricDescription", "String", codec="ZSTD(1)"),
            _col("MetricUnit", "String", codec="ZSTD(1)"),
            _col("Attributes", "Map(LowCardinality(String), String)", codec="ZSTD(1)"),
            _col("StartTimeUnix", "DateTime", codec="Delta(4), ZSTD(1)"),
            _col("TimeUnix", "DateTime", codec="Delta(4), ZSTD(1)"),
            _col("Count", "UInt64", codec="Delta(8), ZSTD(1)"),
            _col("Sum", "Float64", codec="ZSTD(1)"),
            _col("ValueAtQuantiles.Quantile", "Array(Float64)", codec="ZSTD(1)"),
            _col("ValueAtQuantiles.Value", "Array(Float64)", codec="ZSTD(1)"),
            _col("Flags", "UInt32", codec="ZSTD(1)"),
        ],
        config=_otel_config(
            database,
            engine="MergeTree",
            partition_by="toDate(TimeUnix)",
            order_by="ServiceName, MetricName, toStartOfHour(TimeUnix), cityHash64(Attributes), TimeUnix",
            indexes=[
                "INDEX idx_res_attr_key mapKeys(ResourceAttributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_res_attr_value mapValues(ResourceAttributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_scope_attr_key mapKeys(ScopeAttributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_scope_attr_value mapValues(ScopeAttributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_attr_key mapKeys(Attributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_attr_value mapValues(Attributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_time_minmax TimeUnix TYPE minmax GRANULARITY 1",
            ],
        ),
    )


def otel_traces_spec(database: str) -> TableSpec:
    """The exporter's ``otel_traces`` table."""
    return TableSpec(
        name="otel_traces",
        columns=[
            _col("Timestamp", "DateTime64(9)", codec="Delta(8), ZSTD(1)"),
            _col("TraceId", "String", codec="ZSTD(1)"),
            _col("SpanId", "String", codec="ZSTD(1)"),
            _col("ParentSpanId", "String", codec="ZSTD(1)"),
            _col("TraceState", "String", codec="ZSTD(1)"),
            _col("SpanName", "LowCardinality(String)", codec="ZSTD(1)"),
            _col("SpanKind", "LowCardinality(String)", codec="ZSTD(1)"),
            _col("ServiceName", "LowCardinality(String)", codec="ZSTD(1)"),
            _col("ResourceAttributes", "Map(LowCardinality(String), String)", codec="ZSTD(1)"),
            _col("ScopeName", "String", codec="ZSTD(1)"),
            _col("ScopeVersion", "String", codec="ZSTD(1)"),
            _col("SpanAttributes", "Map(LowCardinality(String), String)", codec="ZSTD(1)"),
            _col("Duration", "UInt64", codec="ZSTD(1)"),
            _col("StatusCode", "LowCardinality(String)", codec="ZSTD(1)"),
            _col("StatusMessage", "String", codec="ZSTD(1)"),
            _col("Events.Timestamp", "Array(DateTime64(9))", codec="ZSTD(1)"),
            _col("Events.Name", "Array(LowCardinality(String))", codec="ZSTD(1)"),
            _col(
                "Events.Attributes", "Array(Map(LowCardinality(String), String))", codec="ZSTD(1)"
            ),
            _col("Links.TraceId", "Array(String)", codec="ZSTD(1)"),
            _col("Links.SpanId", "Array(String)", codec="ZSTD(1)"),
            _col("Links.TraceState", "Array(String)", codec="ZSTD(1)"),
            _col("Links.Attributes", "Array(Map(LowCardinality(String), String))", codec="ZSTD(1)"),
        ],
        config=_otel_config(
            database,
            engine="MergeTree",
            partition_by="toDate(Timestamp)",
            order_by="ServiceName, SpanName, toDateTime(Timestamp)",
            indexes=[
                "INDEX idx_trace_id TraceId TYPE bloom_filter(0.001) GRANULARITY 1",
                "INDEX idx_res_attr_key mapKeys(ResourceAttributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_res_attr_value mapValues(ResourceAttributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_span_attr_key mapKeys(SpanAttributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_span_attr_value mapValues(SpanAttributes) TYPE bloom_filter(0.01) GRANULARITY 1",
                "INDEX idx_duration Duration TYPE minmax GRANULARITY 1",
            ],
        ),
    )


def otel_traces_trace_id_ts_spec(database: str) -> TableSpec:
    """The exporter's ``otel_traces_trace_id_ts`` table."""
    return TableSpec(
        name="otel_traces_trace_id_ts",
        columns=[
            _col("TraceId", "String", codec="ZSTD(1)"),
            _col("Start", "DateTime", codec="Delta(4), ZSTD(1)"),
            _col("End", "DateTime", codec="Delta(4), ZSTD(1)"),
        ],
        config=_otel_config(
            database,
            engine="MergeTree",
            partition_by="toDate(Start)",
            order_by="TraceId, Start",
            indexes=[
                "INDEX idx_trace_id TraceId TYPE bloom_filter(0.01) GRANULARITY 1",
            ],
        ),
    )


def _with_dfe_header(spec: TableSpec) -> TableSpec:
    """The DFE deltas from the HyperDX seed, applied to one table.

    Two changes, both about retention rather than query shape:

    ``_timestamp_load`` joins the common header on every otel table. It carries
    ``DEFAULT now64(3)``, so the exporter's INSERT -- which names its own column
    list and knows nothing about it -- still works and the server stamps it at
    insert.

    PARTITION BY moves from event time to load time. Late-arriving telemetry
    writes back into old event-time partitions, scattering small parts and
    reopening closed days; a load-time partition is immutable once the day
    closes, which is also what makes ``ttl_only_drop_parts`` able to drop one.

    ORDER BY is deliberately NOT touched. The DFE data tables lead with
    ``_timestamp_load`` because the loader writes and reads in load order; these
    are queried on event time and service, so fronting load time would destroy
    the access path every HyperDX search uses.
    """
    columns = [
        *spec.columns,
        _col(
            LOAD_TIMESTAMP,
            "DateTime64(3,'UTC')",
            codec="Delta, LZ4",
            default="now64(3)",
            comment="Insertion timestamp (ms precision)",
        ),
    ]
    if spec.name == LOGS_TABLE:
        columns.append(_body_json_column())
    config = replace(spec.config, partition_by=f"toDate({LOAD_TIMESTAMP})")
    return TableSpec(name=spec.name, columns=columns, config=config)


def _body_json_max_paths() -> int:
    """Paths held as typed sub-columns of ``BodyJson`` before the rest spill.

    Read from the common header's ``_json`` column rather than restated, so the
    two JSON columns in the stack cannot drift apart. Paths beyond the limit go
    to a shared map that reads slower, and ClickHouse's guidance is to stay well
    under ~10,000 -- so this is a storage and read-speed trade, not a limit to
    raise on sight. ``otel_logs`` holds every service's log shape in one column
    where a source table holds one family, so if measurement ever moves it, it
    moves UP.
    """
    try:
        for column in SchemaLoader.load_profile(profile_name="timeseries"):
            if column.name == "_json" and column.max_dynamic_paths:
                return int(column.max_dynamic_paths)
    except Exception as exc:
        logger.warning(f"common header unreadable, using the JSON path fallback: {exc}")
    return _FALLBACK_JSON_MAX_PATHS


def _body_json_column() -> SchemaColumn:
    """``Body`` parsed into the native JSON type, for typed queries into it.

    ``Body`` STAYS the text of record. Replacing it is not an option: a plain-text
    line -- a panic, an nginx log, anything not emitted by the chassis -- is not
    valid JSON and a JSON column would reject the whole insert batch. The
    ``isValidJSON`` guard degrades those rows to ``{}`` instead.

    Text alongside parsed JSON is the same call the common header already makes
    with ``_raw`` and ``_json``, both captured from one payload.
    """
    return _col(
        BODY_JSON,
        "JSON",
        max_dynamic_paths=_body_json_max_paths(),
        materialized="CAST(if(isValidJSON(Body), Body, '{}'), 'JSON')",
        comment="Body parsed as JSON; empty for a body that is not JSON",
    )


def trace_id_ts_view_ddl(database: str, on_cluster: str = "") -> str:
    """The MV that maintains the trace-id lookup table.

    Not a TableSpec: a materialised view has no columns to reconcile, only a
    SELECT. Its target table is a spec like any other.
    """
    return (
        f"CREATE MATERIALIZED VIEW IF NOT EXISTS {database}.{TRACE_ID_TS_VIEW}{on_cluster} "
        f"TO {database}.{TRACE_ID_TS_TABLE} AS\n"
        "SELECT TraceId, min(Timestamp) AS Start, max(Timestamp) AS End\n"
        f"FROM {database}.otel_traces\n"
        "WHERE TraceId != ''\n"
        "GROUP BY TraceId"
    )


def otel_specs(database: str) -> list[TableSpec]:
    """Every OTel table, carrying the DFE header deltas. Logs first."""
    return [
        _with_dfe_header(spec)
        for spec in (
            otel_logs_spec(database),
            otel_traces_spec(database),
            otel_traces_trace_id_ts_spec(database),
            otel_metrics_gauge_spec(database),
            otel_metrics_sum_spec(database),
            otel_metrics_histogram_spec(database),
            otel_metrics_exponential_histogram_spec(database),
            otel_metrics_summary_spec(database),
        )
    ]
