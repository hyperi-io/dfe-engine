#  Project:      dfe-engine
#  File:         tests/support/index_use_cases.py
#  Purpose:      The index use case table: one row per documented query shape, with its column and data
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""One row per index use case query, read by the live pruning test and the coverage check.

A row carries everything a new use case needs: the meta schema column, the SQL that
fills it, the query shape from dfe-schemas ``docs/meta-schema.md`` and the row ids
that query must return. The meta schema the test deploys is generated from these
rows, so adding a use case is adding a row.

Every table holds ``ROWS`` rows, one every ``STEP_MS`` of load time over ``DAYS``
days. The needle rows sit inside ``WINDOW`` in one granule, and ``OUTSIDE`` carries
the same needle value outside it, so a query that ignores the window returns it.
"""

import ipaddress
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

ROWS_PER_DAY = 240_000
DAYS = 6
ROWS = ROWS_PER_DAY * DAYS
STEP_MS = 86_400_000 // ROWS_PER_DAY
START = datetime(2026, 10, 1, tzinfo=UTC)

# Day 1 noon to day 4 noon: four of the six daily partitions, two of them cut in half.
WINDOW = range(ROWS_PER_DAY * 3 // 2, ROWS_PER_DAY * 9 // 2)
NEEDLES = (600_000, 600_001, 600_002)
OUTSIDE = 100_000
NEEDLE_ROWS = frozenset(NEEDLES)
# A run of rows inside the window, for the range queries.
SPAN = range(800_000, 801_000)

# The dfe-schemas pruning test's IP generator, offset so row 0 is 10.0.0.0.
_IP_BASE = int(ipaddress.IPv4Address("10.0.0.0"))
_NEEDLE_IP = "192.0.2.1"
_SPAN_DATETIME = datetime(2020, 1, 1, tzinfo=UTC)
_SLASH24 = (_IP_BASE + SPAN.start) & ~0xFF
# A multiplicative hash of the row: every granule spans the whole value range.
_SCATTER = (7919, 1_000_003)


def ch_time(moment: datetime) -> str:
    """A ClickHouse ``DateTime64(3, 'UTC')`` literal for *moment*."""
    return f"toDateTime64('{moment:%Y-%m-%d %H:%M:%S}.{moment.microsecond // 1000:03d}', 3, 'UTC')"


def row_time(row: int) -> datetime:
    """The load time of row *row*."""
    return START + timedelta(milliseconds=row * STEP_MS)


def _ip(row: str) -> str:
    return f"toIPv6(IPv4NumToString(toUInt32({_IP_BASE} + {row})))"


def _ip_literal(row: int) -> str:
    return f"toIPv6('{ipaddress.IPv4Address(_IP_BASE + row)}')"


def _in_window(predicate: Callable[[int], bool]) -> frozenset[int]:
    return frozenset(row for row in WINDOW if predicate(row))


@dataclass(frozen=True, slots=True)
class Column:
    """One meta schema column and the data it holds.

    Attributes:
        name: The column name.
        declare: The rest of its meta schema declaration.
        haystack: The SQL value of an ordinary row, over the row number ``n``.
        needle: The SQL value on the needle rows, or None to keep the haystack.
        outside: The SQL value on the ``OUTSIDE`` row, when it must differ from *needle*.
    """

    name: str
    declare: dict[str, Any]
    haystack: str
    needle: str | None = None
    outside: str | None = None

    @property
    def meta(self) -> dict[str, Any]:
        """The column as a meta schema declares it."""
        return {"name": self.name, **self.declare}

    @property
    def value(self) -> str:
        """The SQL that fills the column for row ``n``."""
        if self.needle is None:
            return self.haystack
        if self.outside is not None:
            rows = ", ".join(str(row) for row in NEEDLES)
            return (
                f"if(n IN ({rows}), {self.needle}, "
                f"if(n = {OUTSIDE}, {self.outside}, {self.haystack}))"
            )
        rows = ", ".join(str(row) for row in (*NEEDLES, OUTSIDE))
        return f"if(n IN ({rows}), {self.needle}, {self.haystack})"


Gate = Literal["prunes", "no_prune", "used", "report"]


@dataclass(frozen=True, slots=True)
class Case:
    """One documented query shape against one column.

    Attributes:
        id: The test id.
        column: The column the query reads, or None for a header column.
        where: The predicate, ANDed with the table's time window.
        expected: The row ids the query must return.
        gate: ``prunes``: the index drops most granules partition and primary key left.
            ``no_prune``: the documented anti-pattern, which the index cannot help.
            ``used``: the plan reads the index, as a vector search does.
            ``report``: measured and printed, never failed.
        tail: What follows the WHERE.
        index: The header column whose index is measured, when *column* is None.
        window: Replaces the header table's time window.
        exact: Settings the row check runs under, for an index whose answer is approximate.
    """

    id: str
    column: Column | None
    where: str
    expected: frozenset[int]
    gate: Gate = "prunes"
    tail: str = "ORDER BY row_id"
    index: str | None = None
    window: str | None = None
    exact: Mapping[str, str] = field(default_factory=dict, hash=False)

    @property
    def index_column(self) -> str:
        """The column whose catalogue indexes this case measures."""
        name = self.column.name if self.column is not None else self.index
        assert name is not None, f"{self.id}: a header case names its index column"
        return name


_TEXT = (
    "concat('event ', toString(n), ' accepted ok')",
    "concat('event ', toString(n), ' connection refused')",
)
_LEVELS = "['info', 'debug', 'warn'][n % 3 + 1]"

LEVEL = Column(
    "level", {"type": "string", "cardinality": "low", "use_case": "dimension"}, _LEVELS, "'error'"
)
# The same column declared not_null, which renders LowCardinality(String).
LEVEL_NOT_NULL = Column(
    "level_not_null",
    {"type": "string", "cardinality": "low", "attribute": ["not_null"], "use_case": "dimension"},
    _LEVELS,
    "'error'",
)
EM_LOW = Column(
    "em_low",
    {"type": "string", "cardinality": "low", "use_case": "exact_match"},
    "['alpha', 'beta', 'gamma'][n % 3 + 1]",
    "'zulu'",
)
EM_HIGH = Column(
    "em_high",
    {"type": "string", "cardinality": "high", "use_case": "exact_match"},
    "concat('id-', toString(n))",
    "'id-needle'",
)
EM_UNKNOWN = Column(
    "em_unknown",
    {"type": "string", "use_case": "exact_match"},
    "concat('user-', toString(n))",
    "'user-needle'",
)
_NEEDLE_UUID = "toUUID('00000000-0000-4000-8000-00000000beef')"
EM_UUID = Column(
    "em_uuid", {"type": "uuid", "use_case": "exact_match"}, "generateUUIDv4(n)", _NEEDLE_UUID
)
EM_INT = Column("em_int", {"type": "integer", "use_case": "exact_match"}, "100000 + n * 7", "4624")
EM_IP = Column(
    "em_ip", {"type": "ip", "use_case": "exact_match"}, _ip("n"), f"toIPv6('{_NEEDLE_IP}')"
)
EM_IP_ARRAY = Column(
    "em_ip_array",
    {"type": "ip", "ch_override": "Array(IPv6)", "use_case": "exact_match"},
    f"[{_ip('n')}, {_ip('n + 5000000')}]",
    f"[toIPv6('10.255.0.1'), toIPv6('{_NEEDLE_IP}')]",
)
RG_INT = Column("rg_int", {"type": "integer", "use_case": "range"}, "5000000 + n")
RG_DATETIME = Column(
    "rg_datetime",
    {"type": "datetime", "use_case": "range"},
    f"addSeconds({ch_time(_SPAN_DATETIME)}, n)",
)
RG_IP = Column("rg_ip", {"type": "ip", "use_case": "range"}, _ip("n"))
RG_UNSORTED = Column(
    "rg_unsorted", {"type": "integer", "use_case": "range"}, f"(n * {_SCATTER[0]}) % {_SCATTER[1]}"
)
WS_TEXT = Column("ws_text", {"type": "text", "use_case": "word_search"}, *_TEXT)
SS_TEXT = Column("ss_text", {"type": "text", "use_case": "substring_search"}, *_TEXT)
KS_MAP = Column(
    "ks_map",
    {"type": "map", "use_case": "key_search"},
    "map(concat('k', toString(n)), concat('v', toString(n)))",
    "map('needle_key', 'needle_val')",
)
# Directions spread over the whole sphere, and needles exactly on the query vector.
# The outside row sits just off it: a vector index returns its k nearest before the
# WHERE runs, so a fourth exact tie outside the window can take a needle's place.
# HNSW is approximate and its graph differs per build, so the rows are checked under
# prefilter, an exact search over the window. The plan check reads the default query.
_VECTOR_EXACT = {"vector_search_filter_strategy": "prefilter"}
SIM_VEC = Column(
    "sim_vec",
    {"type": "vector", "use_case": "similarity_search(4)"},
    "[sin(n * 1.1), cos(n * 1.7), sin(n * 2.3), cos(n * 0.7)]",
    "[0.0, 0.0, 0.0, 1.0]",
    "[0.0, 0.0, 0.1, 1.0]",
)

CASES: tuple[Case, ...] = (
    Case("dimension", LEVEL, "level = 'error'", NEEDLE_ROWS),
    Case("dimension_not_null", LEVEL_NOT_NULL, "level_not_null = 'error'", NEEDLE_ROWS),
    Case("exact_match_low", EM_LOW, "em_low = 'zulu'", NEEDLE_ROWS),
    Case("exact_match_high", EM_HIGH, "em_high = 'id-needle'", NEEDLE_ROWS),
    Case(
        "exact_match_unknown_in",
        EM_UNKNOWN,
        "em_unknown IN ('user-needle', 'user-nobody')",
        NEEDLE_ROWS,
    ),
    Case("exact_match_uuid", EM_UUID, f"em_uuid = {_NEEDLE_UUID}", NEEDLE_ROWS),
    Case("exact_match_integer", EM_INT, "em_int = 4624", NEEDLE_ROWS),
    Case("exact_match_ip", EM_IP, f"em_ip = toIPv6('{_NEEDLE_IP}')", NEEDLE_ROWS),
    Case(
        "exact_match_array_has",
        EM_IP_ARRAY,
        f"has(em_ip_array, toIPv6('{_NEEDLE_IP}'))",
        NEEDLE_ROWS,
    ),
    Case(
        "exact_match_array_has_any",
        EM_IP_ARRAY,
        f"hasAny(em_ip_array, [toIPv6('{_NEEDLE_IP}'), toIPv6('198.51.100.7')])",
        NEEDLE_ROWS,
    ),
    Case(
        "exact_match_array_has_all",
        EM_IP_ARRAY,
        f"hasAll(em_ip_array, [toIPv6('{_NEEDLE_IP}'), toIPv6('10.255.0.1')])",
        NEEDLE_ROWS,
    ),
    Case(
        "range_integer",
        RG_INT,
        f"rg_int BETWEEN {5_000_000 + SPAN.start} AND {5_000_000 + SPAN.stop - 1}",
        frozenset(SPAN),
    ),
    Case(
        "range_datetime",
        RG_DATETIME,
        f"rg_datetime >= {ch_time(_SPAN_DATETIME + timedelta(seconds=SPAN.start))} "
        f"AND rg_datetime < {ch_time(_SPAN_DATETIME + timedelta(seconds=SPAN.stop))}",
        frozenset(SPAN),
    ),
    Case(
        "range_ip",
        RG_IP,
        f"rg_ip BETWEEN {_ip_literal(SPAN.start)} AND {_ip_literal(SPAN.stop - 1)}",
        frozenset(SPAN),
    ),
    # The docs name isIPAddressInRange as the address-range query no index can prune.
    Case(
        "range_ip_in_cidr_anti_pattern",
        RG_IP,
        "isIPAddressInRange(IPv6NumToString(rg_ip), "
        f"'::ffff:{ipaddress.IPv4Address(_SLASH24)}/120')",
        _in_window(lambda row: (_IP_BASE + row) & ~0xFF == _SLASH24),
        gate="no_prune",
    ),
    # minmax prunes only when values arrive roughly in order.
    Case(
        "range_unsorted_anti_pattern",
        RG_UNSORTED,
        "rg_unsorted BETWEEN 0 AND 999",
        _in_window(lambda row: (row * _SCATTER[0]) % _SCATTER[1] <= 999),
        gate="no_prune",
    ),
    Case("word_search_has_token", WS_TEXT, "hasToken(ws_text, 'refused')", NEEDLE_ROWS),
    Case(
        "word_search_has_any_tokens",
        WS_TEXT,
        "hasAnyTokens(ws_text, ['refused', 'denied'])",
        NEEDLE_ROWS,
    ),
    Case(
        "word_search_has_all_tokens",
        WS_TEXT,
        "hasAllTokens(ws_text, ['connection', 'refused'])",
        NEEDLE_ROWS,
    ),
    Case("substring_search_like", SS_TEXT, "ss_text LIKE '%efuse%'", NEEDLE_ROWS),
    Case("key_search_key", KS_MAP, "mapContainsKey(ks_map, 'needle_key')", NEEDLE_ROWS),
    Case("key_search_value", KS_MAP, "has(mapValues(ks_map), 'needle_val')", NEEDLE_ROWS),
    Case(
        "similarity_search_top_k",
        SIM_VEC,
        "1",
        NEEDLE_ROWS,
        gate="used",
        tail=f"ORDER BY cosineDistance(sim_vec, [0.0, 0.0, 0.0, 1.0]) LIMIT {len(NEEDLES)}",
        exact=_VECTOR_EXACT,
    ),
)


def columns() -> list[Column]:
    """Every column the cases read, once each, in the order the cases name them."""
    seen: dict[str, Column] = {}
    for case in CASES:
        if case.column is None:
            continue
        held = seen.setdefault(case.column.name, case.column)
        assert held == case.column, f"two different columns named {case.column.name!r}"
    return list(seen.values())


# The columns the harness owns on every table: the id results are compared by, and
# the event time a table with no header filters on.
HARNESS_COLUMNS: tuple[dict[str, Any], ...] = (
    {"name": "row_id", "type": "integer", "attribute": ["not_null"]},
    {"name": "ts", "type": "timestamp"},
)


def meta_schema() -> dict[str, Any]:
    """The test meta schema: the harness columns, then one column per case."""
    return {
        "current": "1.0.0",
        "versions": {
            "1.0.0": {
                "date": "2026-11-11",
                "type": "model",
                "summary": "One column per index use case",
                "columns": [*HARNESS_COLUMNS, *(column.meta for column in columns())],
            }
        },
    }
