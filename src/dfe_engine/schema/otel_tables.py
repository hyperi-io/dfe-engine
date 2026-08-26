#  Project:      dfe-engine
#  File:         schema/otel_tables.py
#  Purpose:      The OTel telemetry tables, owned by DFE rather than the exporter
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The OTel telemetry tables: logs, traces and the five metric shapes.

Defined in dfe-schemas under ``tables/otel/``; this module only names them and
hands back what the applier works in.

Owning them rather than letting the exporter create them is what makes them
correct on a cluster. The exporter creates over whichever connection the
Service resolved and senses no topology, so the same table ended up plain on
one replica and Replicated on the others: rows written to the plain copy never
replicate, every query succeeds, and the answer depends which replica it landed
on. Through :class:`~dfe_engine.schema.applier.SchemaApplier` the engine clause
is resolved once and applied ON CLUSTER, and the exporter runs with
``create_schema: false``.
"""

from __future__ import annotations

from dfe_engine.schema.schema_ddl import TableSpec
from dfe_engine.schema.table_loader import load_table_spec, load_view_ddl

TRACE_ID_TS_REF = "tables/otel/traces_trace_id_ts"
TRACE_ID_TS_VIEW = "otel_traces_trace_id_ts_mv"

# Logs first: an operator watching the apply wants the table carrying the
# telemetry they are reading confirmed before the metric shapes.
OTEL_TABLE_REFS = (
    "tables/otel/logs",
    "tables/otel/traces",
    TRACE_ID_TS_REF,
    "tables/otel/metrics_gauge",
    "tables/otel/metrics_sum",
    "tables/otel/metrics_histogram",
    "tables/otel/metrics_exponential_histogram",
    "tables/otel/metrics_summary",
)


def trace_id_ts_view_ddl(database: str, on_cluster: str = "") -> str:
    """The MV that maintains the trace-id lookup table."""
    view = load_view_ddl(TRACE_ID_TS_REF, database, on_cluster)
    if view is None:
        raise ValueError(f"{TRACE_ID_TS_REF} declares no materialized_view")
    return view[1]


def otel_specs(database: str) -> list[TableSpec]:
    """Every OTel table, in apply order."""
    return [load_table_spec(ref, database) for ref in OTEL_TABLE_REFS]
