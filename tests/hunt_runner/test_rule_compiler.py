#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_rule_compiler.py
#  Purpose:      A hunt's rule names compile into the INSERT ... SELECT it runs
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""compile_hunt_queries turns rule names into SQL, or into a logged nothing.

Real rule YAML on disk written by the registry that owns that file, so the fixtures
are the on-disk contract rather than a shape invented here. The composed statement
comes from HuntResultSchema, so what is asserted is the wiring - which rule, which
source, which target, and that the window placeholder survives for the worker.
"""

from __future__ import annotations

from pathlib import Path

from dfe_engine.hunt_runner.rule_compiler import compile_hunt_queries
from dfe_engine.hunts.rule_model import Rule
from dfe_engine.hunts.rule_registry import RuleRegistry


def _save_rule(rules_dir: Path, **kwargs) -> None:
    """Write one rule YAML through the registry the API writes it with."""
    registry = RuleRegistry(rules_directory=rules_dir, writable=True, refresh_interval=0)
    try:
        registry.save(Rule(**kwargs))
    finally:
        registry.close()


def test_one_rule_becomes_one_windowed_insert(tmp_path: Path):
    _save_rule(
        tmp_path,
        rule_id="certutil",
        name="Certutil Abuse",
        severity="critical",
        where_clause="process_name = 'certutil.exe'",
    )
    sql = compile_hunt_queries(
        {
            "rules": [{"rule_name": "certutil"}],
            "global_source_table_name": "dfe.main",
            "global_target_table_name": "dfe.detection",
        },
        "windows_hunt",
        rules_dir=tmp_path,
    )
    assert len(sql) == 1
    statement = sql[0]
    assert statement.startswith("INSERT INTO dfe.detection")
    assert "FROM dfe.main" in statement
    assert "WHERE {window} AND (process_name = 'certutil.exe')" in statement
    assert "'certutil' AS rule_id" in statement
    assert "'Certutil Abuse' AS rule_name" in statement
    assert "'critical' AS severity" in statement
    assert "'windows_hunt' AS hunt_name" in statement


def test_each_rule_gets_its_own_statement(tmp_path: Path):
    # Rule id, name and severity are literals in the SELECT, so two rules cannot
    # share one statement without every detection row claiming the same rule.
    _save_rule(tmp_path, rule_id="one", name="One", where_clause="a = 1")
    _save_rule(tmp_path, rule_id="two", name="Two", where_clause="b = 2")
    sql = compile_hunt_queries(
        {
            "rules": [{"rule_name": "one"}, {"rule_name": "two"}],
            "global_source_table_name": "dfe.main",
        },
        "pair",
        rules_dir=tmp_path,
        default_target="dfe.detection",
    )
    assert len(sql) == 2
    assert "'one' AS rule_id" in sql[0]
    assert "'two' AS rule_id" in sql[1]


def test_bare_rule_name_strings_are_accepted(tmp_path: Path):
    _save_rule(tmp_path, rule_id="plain", name="Plain", where_clause="a = 1")
    sql = compile_hunt_queries(
        {"rules": ["plain"], "global_source_table_name": "dfe.main"},
        "hunt",
        rules_dir=tmp_path,
        default_target="dfe.detection",
    )
    assert len(sql) == 1


def test_the_rules_own_source_table_wins_over_the_hunts(tmp_path: Path):
    _save_rule(
        tmp_path,
        rule_id="scoped",
        name="Scoped",
        where_clause="a = 1",
        source_db="acme",
        source_table="windows_audit",
    )
    sql = compile_hunt_queries(
        {"rules": ["scoped"], "global_source_table_name": "dfe.main"},
        "hunt",
        rules_dir=tmp_path,
        default_target="dfe.detection",
    )
    assert "FROM acme.windows_audit" in sql[0]
    assert "'windows_audit' AS source_table" in sql[0]


def test_the_default_target_applies_only_when_nothing_names_one(tmp_path: Path):
    _save_rule(tmp_path, rule_id="r", name="R", where_clause="a = 1")
    definition = {"rules": ["r"], "global_source_table_name": "dfe.main"}
    assert (
        "INSERT INTO dfe.detection"
        in compile_hunt_queries(
            definition, "h", rules_dir=tmp_path, default_target="dfe.detection"
        )[0]
    )
    assert (
        "INSERT INTO other.results"
        in compile_hunt_queries(
            {**definition, "global_target_table_name": "other.results"},
            "h",
            rules_dir=tmp_path,
            default_target="dfe.detection",
        )[0]
    )


def test_a_rule_with_only_sql_is_put_through_the_rewriter(tmp_path: Path):
    # A hand-authored rule YAML carrying the original SELECT and no where_clause:
    # the rewriter extracts the detection logic and strips the time bound, so the
    # rule cannot fight the window predicate the worker substitutes.
    _save_rule(
        tmp_path,
        rule_id="raw",
        name="Raw",
        where_clause="",
        original_sql=(
            "SELECT * FROM acme.events WHERE _timestamp > '2026-01-01' AND action = 'delete'"
        ),
    )
    sql = compile_hunt_queries(
        {"rules": ["raw"]}, "h", rules_dir=tmp_path, default_target="dfe.detection"
    )
    assert "FROM acme.events" in sql[0]
    assert "action = 'delete'" in sql[0]
    assert "2026-01-01" not in sql[0]


def test_a_hunt_with_no_rules_compiles_to_nothing(tmp_path: Path):
    assert compile_hunt_queries({"rules": []}, "h", rules_dir=tmp_path) == []


def test_a_missing_rule_file_is_dropped_not_guessed(tmp_path: Path):
    _save_rule(tmp_path, rule_id="present", name="Present", where_clause="a = 1")
    sql = compile_hunt_queries(
        {"rules": ["absent", "present"], "global_source_table_name": "dfe.main"},
        "h",
        rules_dir=tmp_path,
        default_target="dfe.detection",
    )
    assert len(sql) == 1
    assert "'present' AS rule_id" in sql[0]


def test_a_rule_with_no_detection_logic_is_dropped(tmp_path: Path):
    _save_rule(tmp_path, rule_id="empty", name="Empty", where_clause="")
    assert (
        compile_hunt_queries(
            {"rules": ["empty"], "global_source_table_name": "dfe.main"},
            "h",
            rules_dir=tmp_path,
            default_target="dfe.detection",
        )
        == []
    )


def test_a_rule_with_no_source_is_dropped(tmp_path: Path):
    _save_rule(tmp_path, rule_id="nosource", name="No source", where_clause="a = 1")
    assert (
        compile_hunt_queries(
            {"rules": ["nosource"]}, "h", rules_dir=tmp_path, default_target="dfe.detection"
        )
        == []
    )


def test_an_unqualified_target_lands_beside_its_source(tmp_path: Path):
    _save_rule(tmp_path, rule_id="r", name="R", where_clause="a = 1")
    sql = compile_hunt_queries(
        {
            "rules": ["r"],
            "global_source_table_name": "tenant_a.main",
            "global_target_table_name": "detection",
        },
        "h",
        rules_dir=tmp_path,
    )
    assert sql[0].startswith("INSERT INTO tenant_a.detection")
