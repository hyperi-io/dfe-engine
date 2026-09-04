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

composed by :class:`~dfe_engine.hunts.hunt_output.HuntResultSchema`, so hunt output
keeps the schema's shape instead of a second definition of it. One statement per
rule, because rule id, rule name and severity are literals in the SELECT and a
merged statement could not carry a different set per matched row.

A rule that cannot be compiled (file missing, no detection logic, no source or no
target to resolve) is logged and dropped. A hunt left with NOTHING to run is a
hard failure in the worker rather than a silent clean run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scalo.logger import logger

from dfe_engine.hunts.hunt_output import HuntResultSchema
from dfe_engine.hunts.rule_rewriter import RuleRewriter
from dfe_engine.yaml_utils import yaml_load

WINDOW_TOKEN = "{window}"


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
    where = str(payload.get("where_clause") or "").strip()
    source_db = str(payload.get("source_db") or "")
    source_table = str(payload.get("source_table") or "")
    original_sql = str(payload.get("original_sql") or "")
    if not where and original_sql:
        parsed = rewriter.parse_user_sql(original_sql)
        where = parsed.where_clause.strip()
        source_db = source_db or (parsed.source_db or "")
        source_table = source_table or (parsed.source_table or "")
    return where, source_db, source_table


def compile_hunt_queries(
    definition: dict[str, Any],
    hunt_id: str,
    *,
    rules_dir: str | Path = "",
    default_target: str = "",
) -> list[str]:
    """Compile every rule the hunt names into one INSERT ... SELECT each.

    Args:
        definition: The parsed hunt YAML.
        hunt_id: The hunt's identity (its filename stem), stamped on every row.
        rules_dir: Directory of rule YAML files (``hunts.rules_dir``).
        default_target: ``db.table`` used when neither the rule entry nor the hunt
            names a target. The caller resolves it from the data database.

    Returns:
        The statements, in the hunt's own rule order. Empty when nothing compiled.
    """
    entries = _rule_entries(definition)
    if not entries:
        return []
    directory = Path(rules_dir)
    schema = HuntResultSchema()
    rewriter = RuleRewriter()
    hunt_source = str(definition.get("global_source_table_name") or "")
    hunt_target = str(definition.get("global_target_table_name") or "")

    statements: list[str] = []
    for entry in entries:
        rule_name = str(entry["rule_name"])
        path = directory / f"{rule_name}.yaml"
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

        statements.append(
            schema.build_insert_select(
                target_db=target_db,
                target_table=target_table,
                source_db=source_db,
                source_table=source_table,
                where_clause=where,
                rule_id=rule_name,
                rule_name=str(payload.get("display_name") or payload.get("name") or rule_name),
                hunt_name=hunt_id,
                severity=str(payload.get("severity") or "medium"),
                timestamp_placeholder=WINDOW_TOKEN,
            )
        )
    return statements
