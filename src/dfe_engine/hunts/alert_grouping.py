"""Hunt alert grouping + cooldown — read-time aggregation for alerts.

All matched rows are written to the results table at full fidelity.
Grouping happens at READ TIME when building alerts — a post-INSERT
aggregation query groups by configurable fields and produces one alert
per group with count, time range, and sample data.

Two layers:

Layer 1 — Alert Grouping (group_by):
    After hunt results are written, query the results table with GROUP BY
    on configured fields. 10,000 identical matches → 1 alert per group.

Layer 2 — Dispatcher Cooldown (cooldown):
    Per (hunt, rule, customer, group_key) tracking of last_fired_at.
    Prevents re-alerting within the cooldown window. Cooldown is
    per-group: group A can fire while group B is still in cooldown.

Hunt YAML format::

    alert_grouping:
      group_by:
        - source_ip
        - process_executable
      cooldown: "1h"
      max_alerts_per_run: 50
      max_sample_events: 10
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from hyperi_pylib.logger import logger
from pydantic import BaseModel, Field, field_validator

# ── Duration Parser ──────────────────────────────────────────────

_DURATION_RE = re.compile(r"^(\d+)\s*(s|m|h|d)$", re.IGNORECASE)
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def parse_duration(s: str) -> timedelta:
    """Parse a duration string like '1h', '30m', '7d' into a timedelta.

    Supported units: s (seconds), m (minutes), h (hours), d (days).
    """
    match = _DURATION_RE.match(s.strip())
    if not match:
        raise ValueError(f"Invalid duration: '{s}'. Expected format: <number><s|m|h|d>")
    return timedelta(**{_UNITS[match.group(2).lower()]: int(match.group(1))})


# ── Alert Grouping Config ───────────────────────────────────────


class AlertGroupingConfig(BaseModel):
    """Alert grouping configuration for a hunt."""

    group_by: list[str] = Field(
        default_factory=list,
        description="Field names to GROUP BY for alert aggregation",
    )
    cooldown: str = Field(
        default="1h",
        description="Duration string (e.g. '1h', '30m', '24h') for alert cooldown",
    )
    max_alerts_per_run: int = Field(
        default=0,
        description="Max alerts to fire per execution (0 = unlimited)",
    )
    max_sample_events: int = Field(
        default=10,
        description="Max _json samples included in alert body",
    )

    @field_validator("cooldown")
    @classmethod
    def _validate_cooldown(cls, v: str) -> str:
        parse_duration(v)
        return v

    @property
    def cooldown_td(self) -> timedelta:
        """Return cooldown as a timedelta."""
        return parse_duration(self.cooldown)

    @property
    def has_group_by(self) -> bool:
        """True if group_by fields are configured."""
        return len(self.group_by) > 0


# ── Group Key Builder ──────────────────────────────────────────


def build_group_key(group_by: list[str], group_values: dict[str, str]) -> str:
    """Build a deterministic composite key from group_by field values.

    Format: ``field1=value1|field2=value2`` ordered by the group_by list.
    Returns empty string when group_by is empty (ungrouped alerts).

    Pipe and equals characters in values are escaped so the key is
    unambiguous and invertible.
    """
    if not group_by:
        return ""

    parts: list[str] = []
    for field in group_by:
        raw = group_values.get(field, "")
        escaped = raw.replace("\\", "\\\\").replace("|", "\\|").replace("=", "\\=")
        parts.append(f"{field}={escaped}")
    return "|".join(parts)


# ── Grouping Query Builder ──────────────────────────────────────


def _field_select_expr(field: str, results_table_columns: set[str]) -> tuple[str, str]:
    """Return (select_expression, group_by_expression) for a field.

    If the field exists as a direct column on the results table, use it
    directly. Otherwise extract from _json via JSONExtractString.

    Returns:
        (select_expr, alias) tuple.
    """
    if field in results_table_columns:
        return field, field

    # Extract from _json
    return f"JSONExtractString(_json, '{field}') AS {field}", field


def build_grouping_query(
    target_db: str,
    target_table: str,
    hunt_name: str,
    rule_name: str,
    customer: str,
    group_by: list[str],
    time_start: str,
    time_end: str,
    results_table_columns: set[str],
    max_sample_events: int = 10,
) -> str | None:
    """Build a post-INSERT aggregation query for alert grouping.

    Queries the results table with GROUP BY on the configured fields,
    producing one row per group with match count, time range, and
    a capped sample of _json records.

    Returns None if group_by is empty.
    """
    if not group_by:
        return None

    select_parts: list[str] = []
    group_by_aliases: list[str] = []

    for field in group_by:
        select_expr, alias = _field_select_expr(field, results_table_columns)
        select_parts.append(select_expr)
        group_by_aliases.append(alias)

    # Aggregation columns
    select_parts.extend(
        [
            "count(*) AS match_count",
            "min(_timestamp) AS first_seen",
            "max(_timestamp) AS last_seen",
            f"groupArray({max_sample_events})(_json) AS sample_events",
        ]
    )

    select_clause = ",\n    ".join(select_parts)
    group_by_clause = ", ".join(group_by_aliases)

    # Escape single quotes in string values
    hunt_name_esc = hunt_name.replace("'", "\\'")
    rule_name_esc = rule_name.replace("'", "\\'")
    customer_esc = customer.replace("'", "\\'")

    return (
        f"SELECT\n"
        f"    {select_clause}\n"
        f"FROM {target_db}.{target_table}\n"
        f"WHERE hunt_name = '{hunt_name_esc}'\n"
        f"  AND rule_name = '{rule_name_esc}'\n"
        f"  AND _org_id = '{customer_esc}'\n"
        f"  AND _timestamp >= '{time_start}'\n"
        f"  AND _timestamp < '{time_end}'\n"
        f"GROUP BY {group_by_clause}\n"
        f"ORDER BY match_count DESC"
    )


# ── Alert State Manager ─────────────────────────────────────────

_ALERT_STATE_DDL = """\
CREATE TABLE IF NOT EXISTS {db}.alert_state (
    hunt_name     LowCardinality(String) CODEC(LZ4),
    rule_name     LowCardinality(String) CODEC(LZ4),
    customer_name LowCardinality(String) CODEC(LZ4),
    group_key     String DEFAULT ''      CODEC(ZSTD),
    last_fired_at DateTime               CODEC(DoubleDelta, LZ4),
    fire_count    UInt32 DEFAULT 1        CODEC(Delta, ZSTD),
    suppressed_count UInt64 DEFAULT 0     CODEC(Delta, ZSTD)
) ENGINE = ReplacingMergeTree(last_fired_at)
ORDER BY (hunt_name, rule_name, customer_name, group_key)
TTL last_fired_at + INTERVAL 30 DAY
"""

_CHECK_COOLDOWN_SQL = """\
SELECT last_fired_at
FROM {db}.alert_state FINAL
WHERE hunt_name = %(hunt)s AND rule_name = %(rule)s AND customer_name = %(cust)s
  AND group_key = %(group_key)s
LIMIT 1\
"""

_RECORD_FIRE_SQL = """\
INSERT INTO {db}.alert_state
    (hunt_name, rule_name, customer_name, group_key, last_fired_at, fire_count, suppressed_count)
VALUES\
"""


class AlertStateManager:
    """Manages alert cooldown state in dfe_audit.alert_state.

    Uses ReplacingMergeTree to keep only the latest fire state per
    (hunt_name, rule_name, customer_name). Follows the same pattern
    as HuntCheckpointManager.
    """

    DATABASE = "dfe_audit"

    def __init__(self, database: str | None = None):
        self._db = database or self.DATABASE
        self._table_ensured = False

    def ensure_table_exists(self, ch_client) -> None:
        """Create alert_state table if it doesn't exist (idempotent)."""
        if self._table_ensured:
            return
        try:
            ch_client.execute(f"CREATE DATABASE IF NOT EXISTS {self._db}")
            ch_client.execute(_ALERT_STATE_DDL.format(db=self._db))
            self._table_ensured = True
            logger.debug(f"AlertStateManager: ensured {self._db}.alert_state exists")
        except Exception as e:
            logger.warning(f"AlertStateManager: failed to ensure table: {e}")

    def check_cooldown(
        self,
        ch_client,
        hunt_name: str,
        rule_name: str,
        customer: str,
        cooldown: timedelta,
        group_key: str = "",
    ) -> bool:
        """Check if cooldown has elapsed. Returns True if OK to fire."""
        if cooldown.total_seconds() <= 0:
            return True

        try:
            rows = ch_client.execute(
                _CHECK_COOLDOWN_SQL.format(db=self._db),
                parameters={
                    "hunt": hunt_name,
                    "rule": rule_name,
                    "cust": customer,
                    "group_key": group_key,
                },
            )
            if not rows:
                return True  # Never fired before

            last_fired = rows[0][0]
            if not isinstance(last_fired, datetime):
                last_fired = datetime.fromisoformat(str(last_fired))

            # Ensure timezone-aware comparison
            if last_fired.tzinfo is None:
                last_fired = last_fired.replace(tzinfo=UTC)

            elapsed = datetime.now(UTC) - last_fired
            return elapsed >= cooldown

        except Exception as e:
            logger.warning(f"AlertStateManager: cooldown check failed ({e}), allowing fire")
            return True  # Fail-open: allow alert on error

    def record_fire(
        self,
        ch_client,
        hunt_name: str,
        rule_name: str,
        customer: str,
        group_key: str = "",
        fired_at: datetime | None = None,
        suppressed_count: int = 0,
    ) -> None:
        """Record that an alert was fired."""
        fired_at = fired_at or datetime.now(UTC)
        fired_at_str = fired_at.strftime("%Y-%m-%d %H:%M:%S")

        try:
            ch_client.execute(
                f"{_RECORD_FIRE_SQL.format(db=self._db)}",
                parameters=[
                    [hunt_name, rule_name, customer, group_key, fired_at_str, 1, suppressed_count]
                ],
            )
        except Exception as e:
            logger.warning(f"AlertStateManager: failed to record fire: {e}")

    def get_ddl(self) -> str:
        """Return the DDL string (for DDLFileWriter / reference)."""
        return _ALERT_STATE_DDL.format(db=self._db)
