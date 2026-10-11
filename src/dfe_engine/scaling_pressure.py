#  Project:      dfe-engine
#  File:         scaling_pressure.py
#  Purpose:      The one rule that says which gauge rows ARE scaling pressure
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Which ``otel_metrics_gauge`` rows carry the scaling-pressure composite.

scalo registers the composite as a bare ``scaling_pressure`` gauge, and an app
whose deployment sets a metrics namespace emits ``<namespace>_scaling_pressure``
instead. Matching one exact name would read the apps on one spelling and leave the
KEDA trigger inert on every other.

The contract settled on dfe-infra#302 is that the READER matches whatever prefix
the app used, rather than every Rust app being released to rename its gauge. Both
query catalogues -- the KEDA shim's and the app-management layer's -- carry the
sentinels below where that rule belongs and call :func:`apply` on the SQL, so the
rule is written once here and cannot drift between them.

One app reaches the table as several series: a push over OTLP names its pod in the
resource (``service.instance.id``), and a Prometheus scrape names it in the
data-point attributes. A reader reduces per series and takes the MAX across them,
because averaging a hot pod with an idle one reports neither.
"""

GAUGE = "scaling_pressure"
"""The canonical wire name every prefixed spelling normalises back to."""

MATCH_SENTINEL = "__PRESSURE_MATCH__"
"""What a query catalogue writes where the match predicate belongs."""

NAME_SENTINEL = "__PRESSURE_NAME__"
"""What a query catalogue writes where the normalised metric name belongs."""


def match_sql(column: str = "MetricName") -> str:
    """The predicate matching the bare gauge and any ``<prefix>_`` spelling of it."""
    return f"({column} = '{GAUGE}' OR endsWith({column}, '_{GAUGE}'))"


def name_sql(column: str = "MetricName") -> str:
    """An expression reporting any matched spelling under the canonical bare name."""
    return f"if({match_sql(column)}, '{GAUGE}', {column})"


def apply(sql: str) -> str:
    """Substitute the catalogue sentinels in *sql* with the matching rule."""
    return sql.replace(MATCH_SENTINEL, match_sql()).replace(NAME_SENTINEL, name_sql())


__all__ = ["GAUGE", "MATCH_SENTINEL", "NAME_SENTINEL", "apply", "match_sql", "name_sql"]
