#  Project:      dfe-engine
#  File:         rule_guard.py
#  Purpose:      Refuse a rule that matches every event, and band a rule's measured alert volume
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Guards a detection rule passes before it is saved.

``match_everything`` reads the rule's detection WHERE, with the time window
already taken out, and explains why it matches every event -- or returns None
when the condition narrows anything at all, or cannot be read.

``preview_sql`` counts what the rule matches over a lookback window on its own
source table, and ``volume_verdict`` turns that count into a band and the
plain-language warnings the UI shows beside the saved rule.
"""

from dataclasses import dataclass
from enum import StrEnum

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.simplify import simplify

from ..clickhouse.quoting import quote_identifier
from ..settings import DetectionGuardSettings
from .rule_model import Rule

# Header columns every DFE event carries, so a presence check on one narrows nothing.
ALWAYS_PRESENT_COLUMNS = frozenset({"_json", "_uuid", "_timestamp", "_timestamp_load"})

# One-argument calls that keep a value present when their argument is present.
_PASS_THROUGH_CALLS = frozenset(
    {"tostring", "assumenotnull", "lower", "upper", "lowerutf8", "upperutf8"}
)
_PRESENT_CALLS = frozenset({"notempty", "isnotnull"})
_ABSENT_CALLS = frozenset({"empty", "isnull"})
_LENGTH_CALLS = frozenset({"length", "lengthutf8"})

_MINUTES_PER_DAY = 1440


class VolumeBand(StrEnum):
    """How much a rule's projected alert volume asks of the people reviewing it."""

    OK = "ok"
    GUIDANCE = "guidance"
    WARN = "warn"
    PLAINLY_BAD = "plainly_bad"
    UNMEASURED = "unmeasured"


@dataclass(frozen=True, slots=True)
class VolumeMeasure:
    """What the preview counted over its lookback window.

    Attributes:
        window_minutes: Length of the lookback window.
        total: Events in the source table inside the window.
        matched: Of those, events the rule's condition matched.
    """

    window_minutes: int
    total: int
    matched: int

    @property
    def per_day(self) -> int:
        """Matches projected to a day at the window's rate."""
        return round(self.matched * _MINUTES_PER_DAY / self.window_minutes)

    @property
    def ratio(self) -> float | None:
        """Share of the window's events the rule matched, or None for an empty window."""
        return self.matched / self.total if self.total else None


def source_label(rule: Rule) -> str:
    """The ``<db>.<table>`` a rule scans, for messages."""
    if rule.source_db and rule.source_table:
        return f"{rule.source_db}.{rule.source_table}"
    return rule.source_table or rule.source or "its source"


# -- A rule that matches every event --------------------------------------


def match_everything(where: str, source: str) -> str | None:
    """Say why a detection condition matches every event, or None when it narrows.

    A condition matches everything when it reduces to TRUE: a tautology such as
    ``1=1``, a non-zero literal, or presence checks on header columns every event
    carries (``notEmpty(_json) = 1``, ``_json IS NOT NULL``). An empty condition
    is left to the rule's own execution check, which refuses it.

    Args:
        where: The rule's detection WHERE, time window already removed.
        source: The ``<db>.<table>`` the rule scans, for the message.

    Returns:
        The refusal message, or None when the condition narrows or cannot be read.
    """
    if not where.strip():
        return None
    try:
        tree = sqlglot.parse_one(where, read="clickhouse")
    except SqlglotError, RecursionError:
        return None

    present: list[str] = []

    def replace(node: exp.Expr) -> exp.Expr:
        column = _presence_check(node)
        if column is not None:
            if column not in present:
                present.append(column)
            return exp.true()
        if isinstance(node, (exp.And, exp.Or)):
            node.set("this", _as_condition(node.this))
            node.set("expression", _as_condition(node.expression))
        elif isinstance(node, exp.Not):
            node.set("this", _as_condition(node.this))
        return node

    try:
        reduced = simplify(_as_condition(tree.transform(replace)), dialect="clickhouse")
    except SqlglotError, RecursionError:
        return None
    if not (isinstance(reduced, exp.Boolean) and reduced.this is True):
        return None

    if present:
        names = " and ".join(present)
        verb, pronoun = ("is", "it") if len(present) == 1 else ("are", "them")
        why = (
            f"its condition is true whenever {names} {verb} present, and every event has {pronoun}"
        )
    else:
        why = "its condition is always true"
    return f"This rule matches every event in {source}: {why}. Add a condition that narrows it."


def _unwrap(node: exp.Expr) -> exp.Expr:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _call_name(node: exp.Expr) -> str | None:
    """Lower-cased name of a function call, or None for anything else."""
    if isinstance(node, exp.Anonymous):
        return node.name.lower()
    if isinstance(node, exp.Func):
        return node.sql_name().lower()
    return None


def _call_arg(node: exp.Expr) -> exp.Expr | None:
    """The only argument of a one-argument call, or None."""
    if isinstance(node, exp.Anonymous):
        return node.expressions[0] if len(node.expressions) == 1 else None
    arg = node.this
    return arg if isinstance(arg, exp.Expr) else None


def _header_column(node: exp.Expr) -> str | None:
    """The header column a value reads unchanged, through casts and ``toString``."""
    node = _unwrap(node)
    while True:
        arg = _call_arg(node) if _call_name(node) in _PASS_THROUGH_CALLS else None
        if isinstance(node, exp.Cast):
            node = _unwrap(node.this)
        elif arg is not None:
            node = _unwrap(arg)
        else:
            break
    if not isinstance(node, exp.Column):
        return None
    name = node.name.lower()
    # A header name under a header qualifier is a JSON path, such as _json._uuid.
    if name not in ALWAYS_PRESENT_COLUMNS or node.table.lower() in ALWAYS_PRESENT_COLUMNS:
        return None
    return name


def _number(node: exp.Expr) -> float | None:
    node = _unwrap(node)
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            return float(node.this)
        except ValueError:
            return None
    if isinstance(node, exp.Boolean):
        return 1.0 if node.this else 0.0
    return None


def _presence_check(node: exp.Expr) -> str | None:
    """The header column ``node`` only checks is present, or None."""
    node = _unwrap(node)
    arg = _call_arg(node) if _call_name(node) in _PRESENT_CALLS else None
    if arg is not None:
        return _header_column(arg)

    if isinstance(node, exp.Not):
        inner = _unwrap(node.this)
        if isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null):
            return _header_column(inner.this)
        inner_arg = _call_arg(inner) if _call_name(inner) in _ABSENT_CALLS else None
        return _header_column(inner_arg) if inner_arg is not None else None

    if not isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE)):
        return None
    left, right = _unwrap(node.this), _unwrap(node.expression)
    if isinstance(node, exp.NEQ) and isinstance(right, exp.Literal):
        if right.is_string and right.this == "":
            return _header_column(left)

    value = _number(right)
    left_name = _call_name(left)
    left_arg = _call_arg(left) if left_name is not None else None
    if left_arg is None or value is None:
        return None
    # The comparison reads "the call is non-zero": = 1, != 0, > 0 or >= 1.
    nonzero = (
        (isinstance(node, exp.EQ) and value == 1)
        or (isinstance(node, (exp.NEQ, exp.GT)) and value == 0)
        or (isinstance(node, exp.GTE) and value == 1)
    )
    if left_name in _PRESENT_CALLS and nonzero:
        return _header_column(left_arg)
    if left_name in _LENGTH_CALLS and nonzero and not isinstance(node, exp.EQ):
        return _header_column(left_arg)
    if left_name in _ABSENT_CALLS and isinstance(node, exp.EQ) and value == 0:
        return _header_column(left_arg)
    return None


def _as_condition(node: exp.Expr) -> exp.Expr:
    """Read a bare number the way ClickHouse reads it as a condition: zero is false."""
    value = _number(node)
    if value is not None and not isinstance(_unwrap(node), exp.Boolean):
        return exp.true() if value != 0 else exp.false()
    return node


# -- Measured alert volume ------------------------------------------------

# Aliased dfe_* so neither shadows a source column the rule's condition reads.
_TOTAL = "dfe_total"
_MATCHED = "dfe_matched"


def preview_sql(
    source_db: str, source_table: str, time_column: str, where: str, window_minutes: int
) -> str:
    """The count the volume preview runs: every event in the window, and the matches.

    The condition sits on its own lines so nothing it ends with can swallow the
    rest of the statement.

    Args:
        source_db: Database of the rule's source table.
        source_table: The rule's source table.
        time_column: The load-time column the hunt runner windows on.
        where: The rule's detection WHERE, time window already removed.
        window_minutes: Lookback window length.

    Returns:
        One SELECT returning ``dfe_total`` and ``dfe_matched``.
    """
    return (
        f"SELECT count() AS {_TOTAL}, countIf(\n"
        f"{where}\n"
        f") AS {_MATCHED}\n"
        f"FROM {quote_identifier(source_db)}.{quote_identifier(source_table)}\n"
        f"WHERE {quote_identifier(time_column)} >= now() - INTERVAL {int(window_minutes)} MINUTE"
    )


def preview_settings(guard: DetectionGuardSettings) -> dict[str, float | int | str]:
    """Query settings that bound the preview and make it fail loudly at the bound.

    ``throw`` rather than ``break``: a broken-off aggregate returns zero rows,
    which would read as a rule that matches nothing.
    """
    return {
        "max_execution_time": guard.preview_timeout_seconds,
        "timeout_overflow_mode": "throw",
        "max_rows_to_read": guard.preview_max_rows,
        "read_overflow_mode": "throw",
    }


def volume_verdict(
    measure: VolumeMeasure, guard: DetectionGuardSettings, source: str
) -> tuple[VolumeBand, list[str]]:
    """Band a measured match count and word the warnings for it.

    Args:
        measure: What the preview counted.
        guard: The deployment's thresholds.
        source: The ``<db>.<table>`` the rule scans, for the messages.

    Returns:
        The band, and the warnings to show beside the rule (none when it is OK).
    """
    window = measure.window_minutes
    if measure.total == 0:
        return VolumeBand.UNMEASURED, [
            f"No events reached {source} in the last {window} minutes, so this rule's "
            "alert volume could not be measured."
        ]

    per_day = measure.per_day
    ratio = measure.ratio if measure.total >= guard.min_rows_for_ratio else None
    lead = (
        f"Over the last {window} minutes this rule matched {measure.matched:,} of "
        f"{measure.total:,} events in {source}, about {per_day:,} alerts a day"
    )
    broad = ratio is not None and ratio >= guard.block_match_ratio
    if per_day > guard.block_per_day or (broad and per_day > guard.ack_per_day):
        band = VolumeBand.PLAINLY_BAD
        warnings = [f"{lead} -- far more than a team can review. Narrow it before it runs."]
    elif per_day > guard.ack_per_day:
        band = VolumeBand.WARN
        warnings = [f"{lead} -- consider narrowing it."]
    elif per_day > guard.warn_per_day:
        band = VolumeBand.GUIDANCE
        warnings = [f"{lead} -- more than one analyst typically reviews."]
    else:
        band = VolumeBand.OK
        warnings = []

    if ratio is not None and ratio >= guard.warn_match_ratio:
        warnings.append(
            f"That is {ratio:.0%} of all events in {source}. Check the condition "
            "narrows it to what you meant."
        )
        if band is VolumeBand.OK:
            band = VolumeBand.GUIDANCE
    return band, warnings


def unmeasured_warning(reason: str) -> str:
    """The warning for a preview that could not finish, naming why."""
    return (
        f"The alert-volume preview could not finish: {reason}. This rule's alert "
        "volume is unknown until it runs."
    )
