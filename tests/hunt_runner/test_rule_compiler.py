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

What the capped SQL does on a server is proven against real ClickHouse in
``tests/integration/test_hunt_runner_detection_cap.py``; here it is only the shape.
"""

from pathlib import Path

import pytest
from scalo.logger import logger

from dfe_engine.hunt_runner.rule_compiler import (
    NIL_UUID,
    compile_hunt_queries,
    detection_cap,
)
from dfe_engine.hunts.rule_model import Rule
from dfe_engine.hunts.rule_registry import RuleRegistry
from dfe_engine.settings import MAX_DETECTIONS_PER_RUN


def _save_rule(rules_dir: Path, **kwargs) -> None:
    """Write one rule YAML through the registry the API writes it with."""
    registry = RuleRegistry(rules_directory=rules_dir, writable=True, refresh_interval=0)
    try:
        registry.save(Rule(**kwargs))
    finally:
        registry.close()


def _compiled_sql(*args, **kwargs) -> list[str]:
    """The INSERT text of each compiled statement, for the tests about its shape."""
    return [statement.sql for statement in compile_hunt_queries(*args, **kwargs)]


def test_one_rule_becomes_one_windowed_insert(tmp_path: Path):
    _save_rule(
        tmp_path,
        rule_id="certutil",
        name="Certutil Abuse",
        severity="critical",
        where_clause="process_name = 'certutil.exe'",
    )
    sql = _compiled_sql(
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
    sql = _compiled_sql(
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
    sql = _compiled_sql(
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
    sql = _compiled_sql(
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
        in _compiled_sql(definition, "h", rules_dir=tmp_path, default_target="dfe.detection")[0]
    )
    assert (
        "INSERT INTO other.results"
        in _compiled_sql(
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
    sql = _compiled_sql({"rules": ["raw"]}, "h", rules_dir=tmp_path, default_target="dfe.detection")
    assert "FROM acme.events" in sql[0]
    assert "action = 'delete'" in sql[0]
    assert "2026-01-01" not in sql[0]


@pytest.mark.parametrize(
    ("stored", "compiled"),
    [
        pytest.param(
            "ServiceName = 'dfe-loader' AND {timestamp_condition}",
            "WHERE {window} AND (ServiceName = 'dfe-loader')",
            id="trailing",
        ),
        pytest.param(
            "{timestamp_condition} AND (action = 'delete')",
            "WHERE {window} AND ((action = 'delete'))",
            id="leading-sigma",
        ),
    ],
)
def test_a_stored_time_placeholder_leaves_only_the_window(tmp_path: Path, stored, compiled):
    """The worker fills in only the window; ClickHouse refuses any other placeholder."""
    _save_rule(tmp_path, rule_id="ph", name="Placeholder", where_clause=stored)
    sql = _compiled_sql(
        {"rules": ["ph"], "global_source_table_name": "dfe.main"},
        "h",
        rules_dir=tmp_path,
        default_target="dfe.detection",
    )
    assert compiled in sql[0]
    assert "{timestamp_condition}" not in sql[0]


def test_a_hunt_with_no_rules_compiles_to_nothing(tmp_path: Path):
    assert compile_hunt_queries({"rules": []}, "h", rules_dir=tmp_path) == []


def test_a_missing_rule_file_is_dropped_not_guessed(tmp_path: Path):
    _save_rule(tmp_path, rule_id="present", name="Present", where_clause="a = 1")
    sql = _compiled_sql(
        {"rules": ["absent", "present"], "global_source_table_name": "dfe.main"},
        "h",
        rules_dir=tmp_path,
        default_target="dfe.detection",
    )
    assert len(sql) == 1
    assert "'present' AS rule_id" in sql[0]


@pytest.mark.parametrize("escape", ["../outside", "ABSOLUTE"])
def test_a_rule_name_that_leaves_the_rules_dir_compiles_nothing(tmp_path: Path, escape: str):
    # A real rule file sits one level above the rules dir, so the old join would compile it.
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    _save_rule(tmp_path, rule_id="outside", name="Outside", where_clause="leaked = 1")
    _save_rule(rules_dir, rule_id="present", name="Present", where_clause="a = 1")
    name = str(tmp_path / "outside") if escape == "ABSOLUTE" else escape

    statements = compile_hunt_queries(
        {"rules": [name, "present"], "global_source_table_name": "dfe.main"},
        "h",
        rules_dir=rules_dir,
        default_target="dfe.detection",
    )

    assert [statement.rule_id for statement in statements] == ["present"]
    assert not any("leaked = 1" in statement.sql for statement in statements)


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


# One refused call per family. A rule YAML can be committed straight into the
# deploy repo, so the API's refusal is not the only gate the condition passes.
OFFBOX_CONDITIONS = {
    "url": "url('http://203.0.113.9/leak', 'LineAsString') = 1",
    "s3": "s3('https://203.0.113.9/bucket/key', 'CSV') = 1",
    "file": "file('hostname') LIKE '%a%'",
    "remote": "remote('203.0.113.9', system.users) = 1",
    "dictionary": "dictGet('tenants', 'name', toUInt64(1)) = 'acme'",
    "ai": "aiFilter(toString(_json), 'is it bad') = 1",
    "globalIn": "globalIn(_source, dfe.main)",
    "beside a real condition": "a = 1 AND url('http://203.0.113.9/') = 1",
}


@pytest.mark.parametrize("where", OFFBOX_CONDITIONS.values(), ids=list(OFFBOX_CONDITIONS))
def test_a_hand_edited_rule_that_reads_outside_the_row_compiles_nothing(tmp_path: Path, where):
    _save_rule(tmp_path, rule_id="offbox", name="Off-box", where_clause=where)
    _save_rule(tmp_path, rule_id="present", name="Present", where_clause="a = 1")

    statements = compile_hunt_queries(
        {"rules": ["offbox", "present"], "global_source_table_name": "dfe.main"},
        "h",
        rules_dir=tmp_path,
        default_target="dfe.detection",
    )

    assert [statement.rule_id for statement in statements] == ["present"]
    assert not any(where in statement.sql for statement in statements)
    assert not any(where in statement.count_sql for statement in statements)


def test_the_dropped_rule_is_logged_with_the_call_it_made(tmp_path: Path):
    _save_rule(tmp_path, rule_id="offbox", name="Off-box", where_clause=OFFBOX_CONDITIONS["url"])
    captured: list = []
    handler_id = logger.add(captured.append, level="ERROR")
    try:
        statements = compile_hunt_queries(
            {"rules": ["offbox"], "global_source_table_name": "dfe.main"},
            "h",
            rules_dir=tmp_path,
            default_target="dfe.detection",
        )
    finally:
        logger.remove(handler_id)

    assert statements == []
    assert len(captured) == 1
    assert "may not call url()" in captured[0].record["message"]


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
    sql = _compiled_sql(
        {
            "rules": ["r"],
            "global_source_table_name": "tenant_a.main",
            "global_target_table_name": "detection",
        },
        "h",
        rules_dir=tmp_path,
    )
    assert sql[0].startswith("INSERT INTO tenant_a.detection")


def _flood_hunt(tmp_path: Path, **kwargs):
    _save_rule(
        tmp_path,
        rule_id="flood",
        name="It's Everything",
        severity="low",
        where_clause="_json.kind = 'flood'",
    )
    return compile_hunt_queries(
        {"rules": ["flood"], "global_source_table_name": "acme.main"},
        "noisy",
        rules_dir=tmp_path,
        default_target="acme.detection",
        **kwargs,
    )[0]


def test_every_compiled_rule_is_capped_at_the_shipped_default(tmp_path: Path):
    statement = _flood_hunt(tmp_path)

    assert statement.cap == MAX_DETECTIONS_PER_RUN == 1000
    assert statement.rule_id == "flood"
    # The LIMIT closes the statement, after the rule's own parenthesised WHERE.
    assert statement.sql.endswith("WHERE {window} AND (_json.kind = 'flood')\nLIMIT 1000")


def test_the_cap_given_is_the_limit_compiled(tmp_path: Path):
    statement = _flood_hunt(tmp_path, max_detections=5)

    assert statement.cap == 5
    assert statement.sql.endswith("\nLIMIT 5")


@pytest.mark.parametrize("cap", [0, -1])
def test_a_cap_that_would_write_nothing_is_refused(tmp_path: Path, cap: int):
    with pytest.raises(ValueError, match="at least 1"):
        _flood_hunt(tmp_path, max_detections=cap)


def test_the_count_reads_the_same_window_and_rule_without_a_limit(tmp_path: Path):
    count = _flood_hunt(tmp_path).count_sql

    assert "count() AS dfe_matched" in count
    assert "FROM acme.main" in count
    assert count.endswith("WHERE {window} AND (_json.kind = 'flood')")
    assert "LIMIT" not in count
    # No alias may shadow a column the rule's WHERE reads.
    assert " AS _json" not in count
    assert " AS _org_id" not in count


def test_the_summary_binds_every_value_rather_than_quoting_it(tmp_path: Path):
    statement = _flood_hunt(tmp_path)

    assert statement.summary_sql.startswith("INSERT INTO acme.detection")
    assert f"toUUID('{NIL_UUID}')" in statement.summary_sql
    assert "CAST({dfe_summary:String}, 'JSON')" in statement.summary_sql
    # The apostrophe in the rule name travels as a bound value, so nothing escapes it.
    assert "It's Everything" not in statement.summary_sql
    assert statement.summary_params == {
        "dfe_rule_id": "flood",
        "dfe_rule_name": "It's Everything",
        "dfe_source_table": "main",
        "dfe_hunt_name": "noisy",
        "dfe_severity": "low",
    }


def test_a_cap_within_its_ceiling_is_used_as_configured():
    assert detection_cap(250, 10_000) == 250
    assert detection_cap(10_000, 10_000) == 10_000


def test_a_cap_over_its_ceiling_is_cut_and_says_so():
    captured: list = []
    handler_id = logger.add(captured.append, level="WARNING")
    try:
        assert detection_cap(50_000, 10_000) == 10_000
    finally:
        logger.remove(handler_id)

    assert len(captured) == 1
    extra = captured[0].record["extra"]
    assert extra["max_detections_per_run"] == 50_000
    assert extra["max_detections_per_run_ceiling"] == 10_000
