#  Project:      dfe-engine
#  File:         hunts/rule_condition.py
#  Purpose:      A rule's structured condition and the WHERE clause it compiles to
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A rule's condition as data, so the console can reopen the builder that wrote it.

:class:`RuleCondition` has the match shape a source form uses (a test on a field, or
``all`` and ``any`` groups of them, with ``negate``), widened to the comparisons and
the list test a detection needs. :func:`compile_condition` turns it into the WHERE
fragment the rule stores beside it. Every text operand is quoted with
:func:`~dfe_engine.clickhouse.quoting.quote_literal`, every field is split into
identifier parts that are each quoted with ``quote_identifier``, and numbers are
rendered from Python numbers, so no operand or field name reaches the SQL as text
the caller chose.

A field is a column of the source table when the table has one by that name,
otherwise a path inside the ``_json`` column, which is how the receiver reads a
field too. A path is read as the type of its operand, the way a CEL rule reads
one, because a JSON path holds a different type from row to row.

A test on a missing value is neither true nor false in ClickHouse, so every test
but ``exists`` fails for a row without the field, and ``negate`` does not turn
that into a match. ``exists`` negated is how to find rows that lack a field.
"""

import math
import re
from collections.abc import Callable, Collection, Iterator
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, model_validator

from dfe_engine.clickhouse.quoting import quote_identifier, quote_literal
from dfe_engine.services.schema.json_promotion_service import JSON_COLUMN
from dfe_engine.source.models import MATCH_NESTING_LIMIT

RuleConditionOperator = Literal[
    "equals",
    "not_equals",
    "exists",
    "includes",
    "starts_with",
    "ends_with",
    "lt",
    "lte",
    "gt",
    "gte",
    "in",
]

# What each operator's ``value`` holds, published beside the operator list so the
# console offers the right input for it.
OPERAND_KINDS: dict[str, str] = {
    "equals": "text_or_number",
    "not_equals": "text_or_number",
    "exists": "none",
    "includes": "text",
    "starts_with": "text",
    "ends_with": "text",
    "lt": "number_or_date",
    "lte": "number_or_date",
    "gt": "number_or_date",
    "gte": "number_or_date",
    "in": "list",
}

_COMPARISONS = {
    "equals": "=",
    "not_equals": "!=",
    "lt": "<",
    "lte": "<=",
    "gt": ">",
    "gte": ">=",
}
ORDERINGS = frozenset({"lt", "lte", "gt", "gte"})
"""The operators that compare an order, the ones a time bound is written with."""
_TEXT_TESTS = frozenset({"includes", "starts_with", "ends_with"})

MAX_TESTS = 100
"""The most field tests one condition holds, which bounds the SQL it compiles to."""

MAX_LIST_VALUES = 500
"""The most values an ``in`` test lists."""

MAX_OPERAND_LENGTH = 1024
"""The longest text operand."""

MAX_OPERAND_BYTES = 128 * 1024
"""The most operand bytes one condition holds, keeping its SQL under ClickHouse's 256 KiB max_query_size."""

RESERVED_TOKENS = ("{window}", "{unseen}", "{timestamp_condition}")
"""Placeholders the hunt runner replaces in a rule's text wherever they sit, a quoted operand included."""

MAX_FIELD_PARTS = 16
"""The most dotted parts one field names."""

INT_MIN = -(2**63)
INT_MAX = 2**64 - 1
"""The integers a typed read can hold: Int64 below, UInt64 above."""

_FIELD_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_JSON_PREFIX = f"{JSON_COLUMN}."

type Scalar = str | StrictInt | StrictFloat


class RuleConditionError(ValueError):
    """A condition that cannot compile against its source."""


class RuleCondition(BaseModel):
    """One test on a field, or ``all`` or ``any`` of several, as the rule builder writes it.

    A test compares one ``field`` with an ``operator``. ``all`` holds when every
    part does and ``any`` when at least one does. Each part is a condition of its
    own, so groups nest, and ``negate`` inverts the test or group it sits on.
    """

    # One schema for request and response, which the pruning serializer would otherwise split.
    # The nesting limit is published so the console reads it instead of restating it.
    model_config = ConfigDict(
        extra="forbid",
        json_schema_mode_override="validation",
        json_schema_extra={"x-dfe-max-nesting": MATCH_NESTING_LIMIT},
    )

    field: str | None = Field(
        default=None,
        description=(
            "Field a test reads: a column of the source table, or a dotted path inside the "
            "record as it arrived (e.g. 'tags.collector.type', or '_json.tags.collector.type'). "
            "Each part is letters, digits, '_' and '-', starting with a letter or '_'. "
            "Absent on a group"
        ),
    )
    operator: RuleConditionOperator | None = Field(
        default=None,
        description=(
            "How a test compares ``field`` to ``value``: equals (the default for a test), "
            "not_equals, exists, includes, starts_with, ends_with, lt, lte, gt, gte, in. "
            "Absent on a group"
        ),
        json_schema_extra={"x-dfe-operand-kinds": {**OPERAND_KINDS}},
    )
    value: Scalar | list[Scalar] | None = Field(
        default=None,
        description=(
            "Operand for the operator, by its kind: none for exists, text for includes, "
            "starts_with and ends_with, text or a number for equals and not_equals, a number "
            "or an ISO 8601 date or date-time for lt, lte, gt and gte, a list of texts or of "
            "numbers for in"
        ),
    )
    negate: bool = Field(default=False, description="Invert this test or group")
    all: list[RuleCondition] = Field(
        default_factory=list, description="A group that holds when every part holds"
    )
    any: list[RuleCondition] = Field(
        default_factory=list, description="A group that holds when at least one part holds"
    )

    @model_validator(mode="before")
    @classmethod
    def _equals_by_default(cls, data: Any) -> Any:
        """A test that names no operator compares with ``equals``."""
        if isinstance(data, dict) and data.get("field") is not None and not data.get("operator"):
            return {**data, "operator": "equals"}
        return data

    @model_validator(mode="after")
    def _one_shape(self) -> RuleCondition:
        if self.all and self.any:
            raise ValueError("a condition takes all or any, not both: nest one group in the other")
        if self.all or self.any:
            stray = [k for k in ("field", "operator", "value") if getattr(self, k) is not None]
            if stray:
                raise ValueError(f"{', '.join(stray)} do not apply beside all or any")
            if self.depth() > MATCH_NESTING_LIMIT:
                raise ValueError(f"a condition nests at most {MATCH_NESTING_LIMIT} groups deep")
            if self.test_count() > MAX_TESTS:
                raise ValueError(f"a condition holds at most {MAX_TESTS} tests")
            _check_operand_bytes(self)
            return self
        if self.field is None:
            raise ValueError("a condition tests a field, or groups tests under all or any")
        _check_field(self.field)
        if self.operator is None:
            raise ValueError("a test names an operator")
        _check_operand(self.operator, self.value)
        _check_operand_bytes(self)
        return self

    @property
    def is_test(self) -> bool:
        """Whether this is one test on a field rather than a group."""
        return not (self.all or self.any)

    @property
    def parts(self) -> list[RuleCondition]:
        """A group's parts, empty for a test."""
        return self.all or self.any

    def depth(self) -> int:
        """How many groups nest here: 0 for a test."""
        if self.is_test:
            return 0
        return 1 + max(part.depth() for part in self.parts)

    def test_count(self) -> int:
        """How many field tests this holds."""
        if self.is_test:
            return 1
        return sum(part.test_count() for part in self.parts)

    def operand_bytes(self) -> int:
        """How many bytes of operand this holds, text as UTF-8 and numbers as written."""
        if not self.is_test:
            return sum(part.operand_bytes() for part in self.parts)
        values = self.value if isinstance(self.value, list) else [self.value]
        return sum(len(str(value).encode()) for value in values if value is not None)

    def tests(self) -> Iterator[RuleCondition]:
        """Yield every field test this holds, at any depth.

        Yields:
            Each test as written. A ``negate`` on an enclosing group is not applied.
        """
        if self.is_test:
            yield self
            return
        for part in self.parts:
            yield from part.tests()


def _check_field(field: str) -> None:
    parts = field.split(".")
    if len(parts) > MAX_FIELD_PARTS or not all(_FIELD_PART.fullmatch(p) for p in parts):
        raise ValueError(
            f"field {field!r} is not a column or a dotted path: each part is letters, "
            "digits, '_' and '-', starting with a letter or '_', "
            f"at most {MAX_FIELD_PARTS} parts"
        )


def _check_operand_bytes(condition: RuleCondition) -> None:
    if condition.operand_bytes() > MAX_OPERAND_BYTES:
        raise ValueError(f"a condition's operands total at most {MAX_OPERAND_BYTES} bytes")


def _check_scalar(value: Scalar, operator: str) -> None:
    if isinstance(value, str):
        if len(value) > MAX_OPERAND_LENGTH:
            raise ValueError(
                f"an operand for {operator!r} is at most {MAX_OPERAND_LENGTH} characters"
            )
        if "\x00" in value:
            raise ValueError(f"an operand for {operator!r} holds no NUL character")
        if reserved := next((token for token in RESERVED_TOKENS if token in value), None):
            raise ValueError(
                f"an operand for {operator!r} holds no {reserved}, which the runner fills in"
            )
    elif not math.isfinite(value):
        raise ValueError(f"an operand for {operator!r} is a finite number")
    elif isinstance(value, int) and not INT_MIN <= value <= INT_MAX:
        raise ValueError(f"an integer operand for {operator!r} fits in 64 bits")


def _check_operand(operator: str, value: Scalar | list[Scalar] | None) -> None:
    """Hold ``value`` to the kind ``operator`` takes.

    Raises:
        ValueError: ``value`` is not what ``operator`` compares against.
    """
    kind = OPERAND_KINDS[operator]
    if kind == "none":
        if value is not None:
            raise ValueError(f"{operator!r} compares nothing, so it takes no value")
        return
    if value is None:
        raise ValueError(f"value is required when operator is {operator!r}")
    if kind == "list":
        _check_list(value)
        return
    if isinstance(value, list):
        raise ValueError(f"{operator!r} takes one value, and a list is for 'in'")
    _check_scalar(value, operator)
    if kind == "text" and not isinstance(value, str):
        raise ValueError(f"{operator!r} takes text")
    if kind == "text" and not value:
        raise ValueError(
            f"{operator!r} takes text to look for, and an empty one matches every value"
        )
    if kind == "number_or_date" and isinstance(value, str):
        instant_literal(value)


def _check_list(value: Scalar | list[Scalar]) -> None:
    if not isinstance(value, list):
        raise ValueError("'in' takes a list of values")
    if not value:
        raise ValueError("'in' takes at least one value")
    if len(value) > MAX_LIST_VALUES:
        raise ValueError(f"'in' takes at most {MAX_LIST_VALUES} values")
    if len({isinstance(item, str) for item in value}) > 1:
        raise ValueError("an 'in' list holds texts or numbers, not both")
    for item in value:
        _check_scalar(item, "in")


def instant_literal(value: str) -> str:
    """The ClickHouse expression for an ISO 8601 date or date-time, read as UTC when it names no zone.

    Args:
        value: ``2026-01-31``, ``2026-01-31T09:30:00``, or the same with ``Z`` or an offset.

    Returns:
        A ``toDateTime64`` call at millisecond precision.

    Raises:
        ValueError: ``value`` is not an ISO 8601 date or date-time.
    """
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(f"{value!r} is not a number or an ISO 8601 date or date-time") from None
    parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    stamp = parsed.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return f"toDateTime64({quote_literal(stamp)}, 3, 'UTC')"


def _operand(value: Scalar) -> str:
    if isinstance(value, str):
        return quote_literal(value)
    return str(value) if isinstance(value, int) else repr(value)


def _json_path(path: str) -> str:
    return f"{JSON_COLUMN}." + ".".join(quote_identifier(part) for part in path.split("."))


def _accessor(field: str, columns: Collection[str] | None) -> tuple[str, bool]:
    """The SQL that reads ``field``, and whether it reads a path inside ``_json``.

    Args:
        field: A validated field.
        columns: The source table's columns, or None when they are not known, in
            which case only a name starting with ``_`` is taken for a column.

    Raises:
        RuleConditionError: The field names a column wrongly.
    """
    if field == JSON_COLUMN:
        raise RuleConditionError(f"name a path inside {JSON_COLUMN}, as {JSON_COLUMN}.<path>")
    if field.startswith(_JSON_PREFIX):
        return _json_path(field[len(_JSON_PREFIX) :]), True
    first, _, rest = field.partition(".")
    is_column = first.startswith("_") if columns is None else first in columns
    if columns is not None and first.startswith("_") and not is_column:
        raise RuleConditionError(f"the source table has no column {first!r}")
    if not is_column:
        return _json_path(field), True
    if rest:
        raise RuleConditionError(
            f"{first!r} is a column and holds one value, so {field!r} names nothing inside it"
        )
    return quote_identifier(field), False


def _typed_reads(path: str, sample: Scalar) -> list[str]:
    """The typed reads of a JSON path a test on ``sample`` holds in, in the order they are tried."""
    if isinstance(sample, str):
        return [f"{path}.:String"]
    return [f"{path}.:{kind}" for kind in ("Int64", "UInt64", "Float64")]


def _over_reads(sql: str, in_json: bool, sample: Scalar, render: Callable[[str], str]) -> str:
    """``render`` applied to a column, or to each typed read of a JSON path, first non-null wins."""
    if not in_json:
        return render(sql)
    reads = _typed_reads(sql, sample)
    if len(reads) == 1:
        return render(reads[0])
    return "coalesce(" + ", ".join(render(read) for read in reads) + ")"


def _compile_test(
    field: str, operator: str, value: Scalar | list[Scalar] | None, columns: Collection[str] | None
) -> str:
    sql, in_json = _accessor(field, columns)
    if operator == "exists":
        return f"isNotNull({sql}) AND notEmpty(toString({sql}))"
    if operator in _TEXT_TESTS:
        subject = f"{sql}.:String" if in_json else f"toString({sql})"
        text = quote_literal(str(value))
        if operator == "includes":
            return f"position({subject}, {text}) > 0"
        function = "startsWith" if operator == "starts_with" else "endsWith"
        return f"{function}({subject}, {text})"
    if operator == "in" and isinstance(value, list):
        listed = ", ".join(_operand(item) for item in value)
        return _over_reads(sql, in_json, value[0], lambda read: f"{read} IN ({listed})")
    if value is None or isinstance(value, list):
        raise RuleConditionError(f"operator {operator!r} takes a single value")
    comparison = _COMPARISONS[operator]
    if operator in ORDERINGS and isinstance(value, str):
        subject = f"parseDateTime64BestEffortOrNull({sql}.:String, 3, 'UTC')" if in_json else sql
        return f"{subject} {comparison} {instant_literal(value)}"
    operand = _operand(value)
    return _over_reads(sql, in_json, value, lambda read: f"{read} {comparison} {operand}")


def compile_condition(condition: RuleCondition, columns: Collection[str] | None = None) -> str:
    """The WHERE fragment ``condition`` stands for.

    Args:
        condition: A validated condition.
        columns: The source table's columns, or None when they are not known.

    Returns:
        A boolean SQL expression over the source table, each test and group bracketed.

    Raises:
        RuleConditionError: A field names a column the table lacks, or a path inside one.
    """
    if condition.field is not None and condition.operator is not None:
        text = f"({_compile_test(condition.field, condition.operator, condition.value, columns)})"
    else:
        joiner = " AND " if condition.all else " OR "
        text = "(" + joiner.join(compile_condition(part, columns) for part in condition.parts) + ")"
    return f"NOT {text}" if condition.negate else text
