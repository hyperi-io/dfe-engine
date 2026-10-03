#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_hunt_source_table.py
#  Purpose:      A hunt's source table can only ever name a table
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The runner refuses a source that is not a plain table name, and quotes the rest.

The source is hunt or rule YAML text spliced into ``FROM``, and either file can be
committed straight into the deploy repo, so the compile is a gate in its own
right. ``url(...)`` as a source reads another host, and a name carrying ``) --``
comments out the window and the rule's condition. The address below is in the
documentation range (RFC 5737).
"""

from pathlib import Path

import pytest
from scalo.logger import logger

from dfe_engine.hunt_runner.rule_compiler import compile_hunt_queries
from dfe_engine.hunts.hunt_output import HuntResultSchema
from dfe_engine.hunts.rule_model import Rule
from dfe_engine.hunts.rule_registry import RuleRegistry

# Sources that are not a plain table or database.table, each refused wherever it is named.
REFUSED_SOURCES = {
    "table function": "url('http://203.0.113.9/x', 'JSONEachRow')",
    "remote": "remote('203.0.113.9', system.users)",
    "space": "dfe.main extra",
    "parentheses": "dfe.main()",
    "closing bracket and comment": "dfe.main) -- x",
    "single quote": "dfe.ma'in",
    "double quote": 'dfe."main"',
    "backtick": "`dfe`.`main`",
    "comment marker": "dfe.main -- trailing",
    "block comment": "dfe.main/*x*/",
    "semicolon": "dfe.main; DROP TABLE dfe.main",
    "two dots": "cluster.dfe.main",
    "leading dot": ".main",
    "trailing dot": "dfe.",
    "leading digit": "dfe.1main",
    "leading hyphen": "dfe.-main",
}

# One part of a rule YAML's source_db or source_table that makes the pair no table name.
REFUSED_PARTS = {
    "space": "ma in",
    "closing bracket and comment": "main) -- x",
    "single quote": "ma'in",
    "backtick": "`main`",
    "semicolon": "main; DROP TABLE dfe.main",
    "dot": "a.b",
}


def _save_rule(rules_dir: Path, *, where_clause: str = "a = 1", **fields) -> None:
    registry = RuleRegistry(rules_directory=rules_dir, writable=True, refresh_interval=0)
    try:
        registry.save(
            Rule(rule_id="certutil", name="Certutil", where_clause=where_clause, **fields)
        )
    finally:
        registry.close()


def _compile(rules_dir: Path, *, hunt_source: str = "", entry_source: str = ""):
    entry: dict = {"rule_name": "certutil"}
    if entry_source:
        entry["source_table_name"] = entry_source
    definition: dict = {"rules": [entry], "global_target_table_name": "dfe.detection"}
    if hunt_source:
        definition["global_source_table_name"] = hunt_source
    return compile_hunt_queries(definition, "h", rules_dir=rules_dir)


def _errors(run) -> tuple[object, list[str]]:
    captured: list = []
    handler_id = logger.add(captured.append, level="ERROR")
    try:
        result = run()
    finally:
        logger.remove(handler_id)
    return result, [record.record["message"] for record in captured]


@pytest.mark.parametrize(
    ("where", "source", "quoted"),
    [
        pytest.param("hunt", "dfe.main", "`dfe`.`main`", id="hunt database.table"),
        pytest.param("hunt", "  _tenant_a.main_2  ", "`_tenant_a`.`main_2`", id="hunt padded"),
        pytest.param("entry", "tenant_a.events", "`tenant_a`.`events`", id="rule entry"),
    ],
)
def test_a_plain_source_compiles_quoted_into_the_insert_and_the_count(
    tmp_path: Path, where, source, quoted
):
    _save_rule(tmp_path)

    [statement] = _compile(
        tmp_path,
        hunt_source=source if where == "hunt" else "dfe.main",
        entry_source=source if where == "entry" else "",
    )

    assert f"\nFROM {quoted}\n" in statement.sql
    assert f"\nFROM {quoted}\n" in statement.count_sql
    assert statement.sql.startswith("INSERT INTO `dfe`.`detection`\n")


def test_a_rule_files_bare_database_and_table_compile_quoted(tmp_path: Path):
    _save_rule(tmp_path, source_db="acme", source_table="windows_audit")

    [statement] = _compile(tmp_path, hunt_source="dfe.main")

    assert "\nFROM `acme`.`windows_audit`\n" in statement.sql
    assert "\nFROM `acme`.`windows_audit`\n" in statement.count_sql
    assert statement.summary_params["dfe_source_table"] == "windows_audit"


def test_an_unqualified_source_passes_the_name_check_and_still_needs_a_database(tmp_path: Path):
    _save_rule(tmp_path)

    statements, errors = _errors(lambda: _compile(tmp_path, hunt_source="main"))

    assert statements == []
    assert errors == ["hunt h: rule 'certutil' resolves to no db.table source"]


@pytest.mark.parametrize("where", ["hunt", "entry"])
@pytest.mark.parametrize("source", REFUSED_SOURCES.values(), ids=list(REFUSED_SOURCES))
def test_a_source_that_is_not_a_table_name_compiles_nothing(tmp_path: Path, source, where):
    _save_rule(tmp_path)

    statements, errors = _errors(
        lambda: _compile(
            tmp_path,
            hunt_source=source if where == "hunt" else "dfe.main",
            entry_source=source if where == "entry" else "",
        )
    )

    assert statements == []
    [message] = errors
    assert "source refused" in message
    assert "is not a table name" in message


@pytest.mark.parametrize("part", ["source_db", "source_table"])
@pytest.mark.parametrize("value", REFUSED_PARTS.values(), ids=list(REFUSED_PARTS))
def test_a_rule_file_source_that_is_not_a_table_name_compiles_nothing(tmp_path: Path, part, value):
    source = {"source_db": "acme", "source_table": "events", part: value}
    _save_rule(tmp_path, **source)

    statements, errors = _errors(lambda: _compile(tmp_path, hunt_source="dfe.main"))

    assert statements == []
    [message] = errors
    assert "source refused" in message


def test_a_source_parsed_from_the_rules_own_sql_is_held_to_the_same_rule(tmp_path: Path):
    # The rewriter returns a backtick-quoted name unquoted, so its brackets reach FROM.
    _save_rule(
        tmp_path,
        where_clause="",
        original_sql="SELECT * FROM dfe.`main) -- x` WHERE action = 'delete'",
    )

    statements, errors = _errors(lambda: _compile(tmp_path))

    assert statements == []
    [message] = errors
    assert "source refused" in message
    assert "main) -- x" in message


def test_one_refused_rule_leaves_the_hunts_other_rules_compiling(tmp_path: Path):
    _save_rule(tmp_path)

    statements = compile_hunt_queries(
        {
            "rules": [
                {"rule_name": "certutil", "source_table_name": REFUSED_SOURCES["table function"]},
                {"rule_name": "certutil"},
            ],
            "global_source_table_name": "dfe.main",
            "global_target_table_name": "dfe.detection",
        },
        "h",
        rules_dir=tmp_path,
    )

    assert [statement.sql.split("\nFROM ")[1].splitlines()[0] for statement in statements] == [
        "`dfe`.`main`"
    ]


def test_the_insert_builder_quotes_whatever_source_it_is_handed():
    # Defence in depth: even an unvalidated name can only reach ClickHouse as a table name.
    sql = HuntResultSchema().build_insert_select(
        target_db="dfe",
        target_table="detection",
        source_db="dfe",
        source_table="main) -- `",
        where_clause="a = 1",
        rule_id="r",
        rule_name="R",
        hunt_name="h",
        severity="low",
    )

    assert "\nFROM `dfe`.`main) -- ```\nWHERE " in sql
