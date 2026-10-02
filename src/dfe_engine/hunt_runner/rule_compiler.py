#  Project:      dfe-engine
#  File:         hunt_runner/rule_compiler.py
#  Purpose:      Compile a hunt's rule names into the INSERT ... SELECT it runs
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Turn a hunt's `rules` (rule YAML names) into the statements the worker runs.

A hunt written by ``POST /api/v1/hunts`` carries rule NAMES, never SQL. This is
the missing half of that path: each named rule's detection WHERE clause, plus the
hunt's source and target, become one

    INSERT INTO <target> (...) SELECT ... FROM <source> WHERE {window} AND (<rule>)
    LIMIT <cap>

composed by :class:`~dfe_engine.hunts.hunt_output.HuntResultSchema`, so hunt output
keeps the schema's shape instead of a second definition of it. One statement per
rule, because rule id, rule name and severity are literals in the SELECT and a
merged statement could not carry a different set per matched row.

The LIMIT is the per-rule, per-run detection cap. A rule that matches everything
would otherwise write its whole window into the detection table on every fire.
Each statement also carries the two it needs when it hits that cap: a count of
every match in the window, and the INSERT of one summary row carrying that count,
so a truncated run says how much it left out.

A rule that cannot be compiled (file missing, no detection logic, no source or no
target to resolve) is logged and dropped. A hunt left with NOTHING to run is a
hard failure in the worker rather than a silent clean run.
"""

from pathlib import Path
from typing import Any

from scalo.logger import logger

from dfe_engine.hunts.hunt_output import HuntResultSchema
from dfe_engine.hunts.rule_names import RuleNameError, rule_file
from dfe_engine.hunts.rule_rewriter import RuleRewriter, strip_time_placeholder
from dfe_engine.settings import MAX_DETECTIONS_PER_RUN
from dfe_engine.yaml_utils import yaml_load

from .models import HuntStatement

WINDOW_TOKEN = "{window}"  # noqa: S105, RUF100 - a SQL template placeholder

# The summary row's matched_uuid: no source record, so it can never be taken for a match.
NIL_UUID = "00000000-0000-0000-0000-000000000000"

# The count's column names ARE the summary's runtime parameters, so the worker binds
# one to the other without either restating them. MATCHED stays out of the INSERT:
# the count reaches the row inside the summary JSON the worker builds.
MATCHED = "dfe_matched"
SUMMARY = "dfe_summary"

# How the summary row fills each column the detection INSERT names. The window
# columns come off the count, so the row sorts beside the detections it stands for.
_SUMMARY_VALUES = {
    "_timestamp": "fromUnixTimestamp64Milli({dfe_last_ts:Int64}, 'UTC')",
    "_timestamp_load": "fromUnixTimestamp64Milli({dfe_last_load:Int64}, 'UTC')",
    "_org_id": "{dfe_org:String}",
    "matched_uuid": f"toUUID('{NIL_UUID}')",
    "rule_id": "{dfe_rule_id:String}",
    "rule_name": "{dfe_rule_name:String}",
    "source_table": "{dfe_source_table:String}",
    "hunt_name": "{dfe_hunt_name:String}",
    "severity": "{dfe_severity:String}",
    "_json": f"CAST({{{SUMMARY}:String}}, 'JSON')",
}


def detection_cap(configured: int, ceiling: int) -> int:
    """The cap every compiled rule gets: the configured value, cut to the ceiling.

    Args:
        configured: ``hunts.max_detections_per_run``.
        ceiling: ``hunts.max_detections_per_run_ceiling``.

    Returns:
        The smaller of the two, logged when the ceiling cut it.
    """
    if configured > ceiling:
        logger.warning(
            "hunt detection cap is above its ceiling; using the ceiling",
            max_detections_per_run=configured,
            max_detections_per_run_ceiling=ceiling,
        )
        return ceiling
    return configured


def _split_table(reference: str) -> tuple[str, str]:
    """Split a ``db.table`` reference into its parts; unqualified gives no database."""
    cleaned = (reference or "").strip().replace("`", "")
    if not cleaned:
        return "", ""
    database, _, table = cleaned.rpartition(".")
    return database, table


def _rule_entries(definition: dict[str, Any]) -> list[dict[str, Any]]:
    """The hunt's ``rules`` as dicts, accepting the bare-string form as well."""
    entries: list[dict[str, Any]] = []
    for entry in definition.get("rules") or []:
        if isinstance(entry, str) and entry.strip():
            entries.append({"rule_name": entry.strip()})
        elif isinstance(entry, dict) and entry.get("rule_name"):
            entries.append(dict(entry))
    return entries


def _detection_clause(payload: dict[str, Any], rewriter: RuleRewriter) -> tuple[str, str, str]:
    """A rule's (where_clause, source_db, source_table).

    The registry stores the WHERE the rewriter already extracted. A hand-authored
    rule YAML that carries only the original SELECT is put through the same
    rewriter here, so a time bound written into the rule cannot fight the window.
    """
    where = strip_time_placeholder(str(payload.get("where_clause") or ""))
    source_db = str(payload.get("source_db") or "")
    source_table = str(payload.get("source_table") or "")
    original_sql = str(payload.get("original_sql") or "")
    if not where and original_sql:
        parsed = rewriter.parse_user_sql(original_sql)
        where = parsed.where_clause.strip()
        source_db = source_db or (parsed.source_db or "")
        source_table = source_table or (parsed.source_table or "")
    return where, source_db, source_table


def _count_sql(source_db: str, source_table: str, where: str) -> str:
    """Every match in the window, plus what the summary row is stamped with.

    Aliased ``dfe_*`` so no alias shadows a source column the rule's WHERE reads. The
    org is named only when every match shares one, because the count spans tenants.
    """
    return (
        f"SELECT\n"
        f"    count() AS {MATCHED},\n"
        f"    toUnixTimestamp64Milli(toDateTime64(max(_timestamp), 3)) AS dfe_last_ts,\n"
        f"    toUnixTimestamp64Milli(toDateTime64(max(_timestamp_load), 3)) AS dfe_last_load,\n"
        f"    if(min(_org_id) = max(_org_id), min(_org_id), '') AS dfe_org\n"
        f"FROM {source_db}.{source_table}\n"
        f"WHERE {WINDOW_TOKEN} AND ({where})"
    )


def _summary_sql(schema: HuntResultSchema, target_db: str, target_table: str) -> str:
    """The one summary row's INSERT. Every value is a bound parameter, never a literal."""
    columns = schema.insert_columns()
    values = ",\n    ".join(_SUMMARY_VALUES[column] for column in columns)
    return (
        f"INSERT INTO {target_db}.{target_table}\n    ({', '.join(columns)})\nSELECT\n    {values}"
    )


def compile_hunt_queries(
    definition: dict[str, Any],
    hunt_id: str,
    *,
    rules_dir: str | Path = "",
    default_target: str = "",
    max_detections: int = MAX_DETECTIONS_PER_RUN,
) -> list[HuntStatement]:
    """Compile every rule the hunt names into one capped INSERT ... SELECT each.

    Args:
        definition: The parsed hunt YAML.
        hunt_id: The hunt's identity (its filename stem), stamped on every row.
        rules_dir: Directory of rule YAML files (``hunts.rules_dir``).
        default_target: ``db.table`` used when neither the rule entry nor the hunt
            names a target. The caller resolves it from the data database.
        max_detections: Detection rows each rule may write in one run, already cut
            to the ceiling by :func:`detection_cap`.

    Returns:
        The statements, in the hunt's own rule order. Empty when nothing compiled.

    Raises:
        ValueError: ``max_detections`` is below 1, which would write nothing at all.
    """
    if max_detections < 1:
        raise ValueError(f"max_detections must be at least 1, not {max_detections}")
    entries = _rule_entries(definition)
    if not entries:
        return []
    directory = Path(rules_dir)
    schema = HuntResultSchema()
    rewriter = RuleRewriter()
    hunt_source = str(definition.get("global_source_table_name") or "")
    hunt_target = str(definition.get("global_target_table_name") or "")

    statements: list[HuntStatement] = []
    for entry in entries:
        rule_name = str(entry["rule_name"])
        try:
            path = rule_file(directory, rule_name, ".yaml")
        except RuleNameError as exc:
            logger.error(f"hunt {hunt_id}: rule '{rule_name}' refused: {exc}")
            continue
        try:
            payload = yaml_load(path)
        except Exception as exc:
            logger.error(f"hunt {hunt_id}: rule '{rule_name}' unreadable at {path}: {exc}")
            continue
        if not isinstance(payload, dict):
            logger.error(f"hunt {hunt_id}: rule '{rule_name}' is not a YAML mapping")
            continue

        where, rule_db, rule_table = _detection_clause(payload, rewriter)
        if not where:
            logger.error(f"hunt {hunt_id}: rule '{rule_name}' has no detection logic")
            continue

        source_db, source_table = _split_table(
            str(entry.get("source_table_name") or "")
            or (f"{rule_db}.{rule_table}" if rule_table else "")
            or hunt_source
        )
        if not source_db or not source_table:
            logger.error(f"hunt {hunt_id}: rule '{rule_name}' resolves to no db.table source")
            continue

        target_db, target_table = _split_table(
            str(entry.get("target_table_name") or "") or hunt_target or default_target
        )
        # An unqualified target lands beside its source, never in a guessed database.
        target_db = target_db or source_db
        if not target_table:
            logger.error(f"hunt {hunt_id}: rule '{rule_name}' resolves to no target table")
            continue

        display_name = str(payload.get("display_name") or payload.get("name") or rule_name)
        severity = str(payload.get("severity") or "medium")
        insert = schema.build_insert_select(
            target_db=target_db,
            target_table=target_table,
            source_db=source_db,
            source_table=source_table,
            where_clause=where,
            rule_id=rule_name,
            rule_name=display_name,
            hunt_name=hunt_id,
            severity=severity,
            timestamp_placeholder=WINDOW_TOKEN,
        )
        statements.append(
            HuntStatement(
                sql=f"{insert}\nLIMIT {max_detections}",
                rule_id=rule_name,
                cap=max_detections,
                count_sql=_count_sql(source_db, source_table, where),
                summary_sql=_summary_sql(schema, target_db, target_table),
                summary_params={
                    "dfe_rule_id": rule_name,
                    "dfe_rule_name": display_name,
                    "dfe_source_table": source_table,
                    "dfe_hunt_name": hunt_id,
                    "dfe_severity": severity,
                },
            )
        )
    return statements
