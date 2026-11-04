#  Project:      dfe-engine
#  File:         hunts/hunt_spec.py
#  Purpose:      Hunt spec models
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Hunt definition, as loaded from the gitops config."""

from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from dfe_engine.hunts.checkpoint import TIMESTAMP_FIELD


class HuntStatement(BaseModel):
    """One statement a hunt runs, and what the worker does when it hits its cap.

    A statement compiled from a rule carries a LIMIT of ``cap`` and the two
    statements that record a truncation: ``count_sql`` counts every match in the
    window, ``summary_sql`` writes the one summary row from that count. A direct
    ``query`` hunt is its own SQL, so it has no cap (``cap`` 0) and neither of them.

    Attributes:
        sql: The INSERT ... SELECT, carrying a ``{window}`` placeholder, and an
            ``{unseen}`` one when it has a target.
        rule_id: The rule it was compiled from, empty for a direct query.
        cap: Detection rows the statement may write in one run, 0 for uncapped.
        count_sql: SELECT of the true match count, carrying ``{window}``. Its column
            names are the runtime parameters ``summary_sql`` binds.
        summary_sql: INSERT of the one summary row, fully parameterised.
        summary_params: The compile-time parameters ``summary_sql`` binds.
        source: The (database, table) a compiled rule reads, which the safe horizon
            checks. None for a direct query.
        target: The (database, table) a compiled rule writes its detections to. A
            statement with one carries an ``{unseen}`` placeholder, so it reads the
            overlap and skips what it already detected. None for a direct query.
    """

    model_config = ConfigDict(frozen=True)

    sql: str
    rule_id: str = ""
    cap: int = 0
    count_sql: str = ""
    summary_sql: str = ""
    summary_params: dict[str, str] = {}
    source: tuple[str, str] | None = None
    target: tuple[str, str] | None = None


class HuntSpec(BaseModel):
    """A hunt definition (gitops config). interval_seconds is derived from cron."""

    hunt_id: str
    interval_seconds: int
    # Every statement this hunt runs, each carrying its own INSERT target and a
    # {window} placeholder. One per rule when the hunt was compiled from `rules`.
    queries: list[HuntStatement] = []
    # The org names a rules hunt reads, or None for a direct query, which is its own SQL.
    customers: list[str] | None = None
    # Its customers the engine stored no tenant ids for, so every fire is refused.
    unresolved_customers: list[str] = []
    # Optional target metadata. Empty by default - the query carries its own target,
    # resolved against effective_data_database by config; never hardcode 'dfe' here.
    target_table: str = ""
    # The hunt YAML's `alerts` block: destinations, raw channels and CEL triggers.
    alerts: dict[str, Any] = {}
    # FIXED watermark field - the always-present common-header column.
    timestamp_field: str = TIMESTAMP_FIELD

    @field_validator("queries", mode="before")
    @classmethod
    def _bare_sql_is_an_uncapped_statement(cls, value: Any) -> Any:
        """Accept a bare SQL string as a statement with no cap, the direct-query form."""
        if not isinstance(value, list):
            return value
        return [HuntStatement(sql=item) if isinstance(item, str) else item for item in value]
