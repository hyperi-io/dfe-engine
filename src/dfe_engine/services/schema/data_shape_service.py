#  Project:      dfe-engine
#  File:         src/dfe_engine/services/schema/data_shape_service.py
#  Purpose:      Measure a column's data shape from rows that have already landed
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Data shape is detected, not asked for.

A DFE primitive plus a use-case label goes in, and an opinionated ClickHouse
implementation comes out. That only works where DFE knows what the data looks
like, and it already can: at promote time the field is sitting in ``_json``, and
at derived-selection time the source table exists with real rows. So ask
ClickHouse rather than infer from a type somebody was handed.

Cardinality is the first property and the only one built. It is one fact that
used to have two homes -- ``attribute: [lowcardinality]`` set the storage
wrapper by hand, ``exact_match`` picked ``set(0)`` over ``bloom_filter``
separately -- and nothing kept them agreeing.

Three rules every property here obeys:

* **``unknown`` is a real value and the honest default.** Where no data exists
  yet, the reading says so. Nobody re-reviews a field that already looks
  decided, so a draft that admits it does not know beats one that is
  confidently wrong.
* **A reading is not a fact.** Cardinality moves, so what was measured, how many
  rows it covers and when it was taken travel with the value.
* **Sample, do not scan.** ``otel_logs`` runs 116G a shard.

Adding one of the eight properties still unbuilt -- null ratio, value length
spread, numeric min/max, monotonicity, run length, format sniffing, array-ness
-- is a :class:`ShapeProbe` subclass and a :data:`PROBES` entry. A probe names
the SELECT terms it needs and reads its own columns back out of the same
bounded read, so a second property costs no second scan.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from dfe_engine.schema.schema_ddl import quote_ident
from dfe_engine.services.schema.json_promotion_service import (
    JSON_COLUMN,
    JsonPromotionError,
    json_subcolumn,
    match_condition,
    qualified_table,
)
from dfe_engine.source.type_registry import DEFAULT_CARDINALITY

__all__ = [
    "DEFAULT_SAMPLE_ROWS",
    "LOW_CARDINALITY_CEILING",
    "PROBES",
    "CardinalityProbe",
    "DataShapeError",
    "Reading",
    "ShapeProbe",
    "column_accessor",
    "json_path_accessor",
    "match_read_columns",
    "measure_cardinality",
    "measure_shape",
]

# ClickHouse's dictionary pays below roughly 10,000 distinct values and costs
# more than it saves above it; the same number dfe-schemas docs/meta-schema.md
# quotes for the wrapper this decides.
LOW_CARDINALITY_CEILING = 10_000

# Rows one measurement reads. No DFE table declares a sampling key, so `SAMPLE`
# is unavailable and the bound is a subquery LIMIT plus a hard read cap.
DEFAULT_SAMPLE_ROWS = 100_000


class DataShapeError(Exception):
    """A shape measurement could not be taken."""


@dataclass(frozen=True)
class Reading:
    """One data-shape property, read off real rows at a point in time.

    ``value`` is the column property this implies -- for cardinality, one of
    ``low``, ``high`` or ``unknown``. ``rows`` is how many rows the reading
    covers and ``detail`` carries the property's own numbers, so a stale reading
    can be recognised as one rather than read as truth.
    """

    property: str
    value: str
    rows: int
    measured_at: str
    detail: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """The reading as plain JSON-safe data, for an API response or a log."""
        return {
            "property": self.property,
            "value": self.value,
            "rows": self.rows,
            "measured_at": self.measured_at,
            "detail": dict(self.detail),
        }


class ShapeProbe:
    """One measurable data-shape property.

    ``terms`` names the SELECT expressions the probe needs over an accessor,
    keyed by the alias they are read back under; ``read`` turns those values
    plus the row count into the property they decide. Every probe in one
    measurement shares one bounded read.
    """

    name = ""

    def terms(self, accessor: str) -> dict[str, str]:
        """Alias -> SQL expression over *accessor*."""
        raise NotImplementedError

    def read(self, values: Mapping[str, Any], rows: int) -> tuple[str, dict[str, int]]:
        """The declared value this probe measured, plus its own numbers."""
        raise NotImplementedError


class CardinalityProbe(ShapeProbe):
    """How many distinct values the column holds.

    ``high`` is proof at any sample size: having seen more than the ceiling
    distinct values, the column has more than the ceiling. ``low`` is only
    honest once the sample was big enough to have exceeded the ceiling --
    below that, a low count is an artefact of how little was read, so the
    reading stays ``unknown``.
    """

    name = "cardinality"

    def __init__(self, *, ceiling: int = LOW_CARDINALITY_CEILING) -> None:
        self._ceiling = ceiling

    def terms(self, accessor: str) -> dict[str, str]:
        return {"distinct_values": f"uniqCombined({accessor})"}

    def read(self, values: Mapping[str, Any], rows: int) -> tuple[str, dict[str, int]]:
        distinct = int(values["distinct_values"] or 0)
        detail = {"distinct_values": distinct, "ceiling": self._ceiling}
        if rows == 0:
            return DEFAULT_CARDINALITY, detail
        if distinct > self._ceiling:
            return "high", detail
        if rows > self._ceiling:
            return "low", detail
        return DEFAULT_CARDINALITY, detail


PROBES: tuple[ShapeProbe, ...] = (CardinalityProbe(),)


def json_path_accessor(path: str, *, json_column: str = JSON_COLUMN) -> str:
    """SQL reading a dotted JSON path out of the payload column.

    The spelling the promotion service already uses, so the engine has one way
    to read a JSON path rather than two that can disagree.
    """
    if json_column != JSON_COLUMN:
        raise DataShapeError(f"the payload column is {JSON_COLUMN!r}, not {json_column!r}")
    try:
        return json_subcolumn(path)
    except JsonPromotionError as exc:
        raise DataShapeError(str(exc)) from exc


def column_accessor(name: str) -> str:
    """SQL reading an existing column, for a table that already carries it."""
    return quote_ident(name, what="column name")


def match_read_columns(match_field: str | None) -> list[str]:
    """The columns the inner SELECT must carry for a match rule to read.

    A ``_``-prefixed field is a header column on the landing table; anything
    else is a path inside the payload column, which is how the receiver's
    router reads it.
    """
    if not match_field:
        return []
    if match_field.startswith(f"{JSON_COLUMN}.") or not match_field.startswith("_"):
        return [JSON_COLUMN]
    return [match_field]


def measure_shape(
    client: Any,
    *,
    db: str,
    table: str,
    accessor: str,
    read_columns: Sequence[str],
    match_field: str | None = None,
    match_value: str | None = None,
    match_operator: str = "equals",
    sample_rows: int = DEFAULT_SAMPLE_ROWS,
    probes: Sequence[ShapeProbe] = PROBES,
    now: datetime | None = None,
) -> list[Reading]:
    """Every probe's reading of one column or JSON path, over one bounded read.

    ``read_columns`` names the columns the inner SELECT has to carry -- the
    payload column for a JSON path, the column itself otherwise, plus whatever
    the match rule compares. The inner ``LIMIT`` is what bounds the read: no
    DFE table declares a sampling key, so ``SAMPLE`` raises SAMPLING_NOT_SUPPORTED
    on every one of them, and ``max_rows_to_read`` with ``read_overflow_mode``
    caps what a selective match can still walk.

    ``match_field``/``match_value`` scope the rows to one source's match rule,
    for measuring against the shared landing table before the source has a
    table of its own.

    Raises:
        DataShapeError: when the underlying ClickHouse query fails.
    """
    if not probes:
        raise DataShapeError("no probes to measure with")
    if not read_columns:
        raise DataShapeError("the inner read names no columns")
    limit = int(sample_rows)
    if limit < 1:
        raise DataShapeError(f"sample_rows must be positive, not {sample_rows!r}")

    target = qualified_table(db, table)
    match_sql, params = match_condition(match_field, match_value, match_operator=match_operator)

    selected: dict[str, str] = {}
    for probe in probes:
        for alias, expression in probe.terms(accessor).items():
            selected[f"{probe.name}__{alias}"] = expression

    inner_columns = ", ".join(quote_ident(name, what="column name") for name in read_columns)
    inner = f"SELECT {inner_columns} FROM {target}"  # noqa: S608 - columns and table pass quote_ident and qualified_table
    if match_sql:
        inner += f" WHERE {match_sql}"
    inner += " LIMIT {sample_rows:UInt64}"

    outer_terms = ", ".join(f"{sql} AS {alias}" for alias, sql in selected.items())
    # The setting takes no query parameter, so the cap is spliced as the int it
    # was coerced to above -- never operator text.
    sql = (
        f"SELECT {outer_terms}, count() AS rows FROM ({inner}) "  # noqa: S608 - probe terms are code over a quoted accessor; limit is an int
        f"SETTINGS max_rows_to_read = {limit}, read_overflow_mode = 'break'"
    )

    try:
        columns, rows = client.query_rows(sql, parameters={**params, "sample_rows": limit})
    except Exception as exc:
        raise DataShapeError(f"shape measurement failed: {exc}") from exc
    if not rows:
        raise DataShapeError("shape measurement returned no row")

    values = dict(zip(columns, rows[0], strict=True))
    row_count = int(values["rows"] or 0)
    measured_at = (now or datetime.now(UTC)).isoformat()

    readings: list[Reading] = []
    for probe in probes:
        prefix = f"{probe.name}__"
        own = {key[len(prefix) :]: value for key, value in values.items() if key.startswith(prefix)}
        value, detail = probe.read(own, row_count)
        readings.append(
            Reading(
                property=probe.name,
                value=value,
                rows=row_count,
                measured_at=measured_at,
                detail=detail,
            )
        )
    return readings


def measure_cardinality(
    client: Any,
    *,
    db: str,
    table: str,
    column: str | None = None,
    json_path: str | None = None,
    match_field: str | None = None,
    match_value: str | None = None,
    match_operator: str = "equals",
    sample_rows: int = DEFAULT_SAMPLE_ROWS,
    ceiling: int = LOW_CARDINALITY_CEILING,
    now: datetime | None = None,
) -> Reading:
    """One cardinality reading, for an existing column or a path inside ``_json``.

    Exactly one of ``column`` and ``json_path``. The reading carries the
    distinct count, the rows it covers and the date it was taken -- record it
    against the column rather than treating it as settled.

    Raises:
        DataShapeError: when neither or both are named, or the query fails.
    """
    if bool(column) == bool(json_path):
        raise DataShapeError("name exactly one of column and json_path")

    if json_path is not None:
        accessor = json_path_accessor(json_path)
        wanted = [JSON_COLUMN]
    else:
        accessor = column_accessor(str(column))
        wanted = [str(column)]

    # dict.fromkeys keeps the order and drops a column the match already names.
    read_columns = list(dict.fromkeys([*wanted, *match_read_columns(match_field)]))

    readings = measure_shape(
        client,
        db=db,
        table=table,
        accessor=accessor,
        read_columns=read_columns,
        match_field=match_field,
        match_value=match_value,
        match_operator=match_operator,
        sample_rows=sample_rows,
        probes=(CardinalityProbe(ceiling=ceiling),),
        now=now,
    )
    return readings[0]
