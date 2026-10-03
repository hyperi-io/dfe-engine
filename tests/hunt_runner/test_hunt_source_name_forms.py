#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_hunt_source_name_forms.py
#  Purpose:      A hunt over a hyphenated or unqualified source compiles to a quoted FROM
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Sources named for a DNS-1123 label, or with no database, compile end to end.

A source table takes its source's label as its name, so ``dfe.cisco-ios`` is the
ordinary case, and both parts are backtick-quoted into ``FROM``. A source named
without a database is read from the data database the runner's CLI hands the
loader. A results table is still held to letters, digits and ``_``.
"""

from pathlib import Path

import pytest
from scalo.logger import logger

from dfe_engine.hunt_runner import cli
from dfe_engine.hunt_runner.rule_compiler import compile_hunt_queries
from dfe_engine.hunt_runner.spec_loader import load_specs
from dfe_engine.hunts.rule_model import Rule
from dfe_engine.hunts.rule_registry import RuleRegistry
from dfe_engine.settings import DFESettings
from dfe_engine.yaml_utils import yaml_dump_string


def _save_rule(rules_dir: Path, **fields) -> None:
    registry = RuleRegistry(rules_directory=rules_dir, writable=True, refresh_interval=0)
    try:
        registry.save(Rule(rule_id="certutil", name="Certutil", where_clause="a = 1", **fields))
    finally:
        registry.close()


def _errors(run) -> tuple[object, list[str]]:
    captured: list = []
    handler_id = logger.add(captured.append, level="ERROR")
    try:
        result = run()
    finally:
        logger.remove(handler_id)
    return result, [record.record["message"] for record in captured]


def _write_hunt(hunt_dir: Path, name: str, **fields) -> None:
    hunt_dir.mkdir(parents=True, exist_ok=True)
    definition = {"cron": "*/5 * * * *", "rules": [{"rule_name": "certutil"}], **fields}
    (hunt_dir / f"{name}.yaml").write_text(
        yaml_dump_string(definition), encoding="utf-8", newline="\n"
    )


def _runner_sources(rules_dir: Path, database: str) -> dict:
    settings = DFESettings(env="test")
    settings.hunts.rules_dir = str(rules_dir)
    return cli._spec_sources(settings, database)


@pytest.mark.parametrize(
    ("where", "source", "quoted"),
    [
        pytest.param("hunt", "dfe.cisco-ios", "`dfe`.`cisco-ios`", id="hunt"),
        pytest.param("entry", "dfe.cisco-meraki", "`dfe`.`cisco-meraki`", id="rule entry"),
        pytest.param("hunt", "tenant-a.cisco-ios", "`tenant-a`.`cisco-ios`", id="database"),
    ],
)
def test_a_hyphenated_source_compiles_quoted(tmp_path: Path, where, source, quoted):
    _save_rule(tmp_path)
    entry: dict = {"rule_name": "certutil"}
    if where == "entry":
        entry["source_table_name"] = source
    definition = {
        "rules": [entry],
        "global_source_table_name": source if where == "hunt" else "dfe.main",
        "global_target_table_name": "dfe.detection",
    }

    statements, errors = _errors(lambda: compile_hunt_queries(definition, "h", rules_dir=tmp_path))

    assert errors == []
    [statement] = statements
    assert f"\nFROM {quoted}\n" in statement.sql
    assert f"\nFROM {quoted}\n" in statement.count_sql


def test_a_rule_files_hyphenated_table_compiles_quoted(tmp_path: Path):
    _save_rule(tmp_path, source_db="dfe", source_table="windows-audit_sigma")

    [statement] = compile_hunt_queries(
        {"rules": ["certutil"], "global_target_table_name": "dfe.detection"},
        "h",
        rules_dir=tmp_path,
    )

    assert "\nFROM `dfe`.`windows-audit_sigma`\n" in statement.sql
    assert statement.summary_params["dfe_source_table"] == "windows-audit_sigma"


def test_a_hyphenated_target_is_still_refused(tmp_path: Path):
    _save_rule(tmp_path)

    statements, errors = _errors(
        lambda: compile_hunt_queries(
            {
                "rules": ["certutil"],
                "global_source_table_name": "dfe.cisco-ios",
                "global_target_table_name": "dfe.detection-results",
            },
            "h",
            rules_dir=tmp_path,
        )
    )

    assert statements == []
    [message] = errors
    assert "target refused" in message


@pytest.mark.parametrize(
    ("source", "quoted"),
    [
        pytest.param("main", "`tenant_a`.`main`", id="bare table"),
        pytest.param("cisco-ios", "`tenant_a`.`cisco-ios`", id="bare hyphenated table"),
        pytest.param("other.main", "`other`.`main`", id="its own database wins"),
    ],
)
def test_the_runner_reads_an_unqualified_source_from_its_data_database(
    tmp_path: Path, source, quoted
):
    rules_dir = tmp_path / "rules"
    _save_rule(rules_dir)
    _write_hunt(tmp_path / "hunts", "bare", global_source_table_name=source)

    specs, errors = _errors(
        lambda: load_specs(tmp_path / "hunts", **_runner_sources(rules_dir, "tenant_a"))
    )

    assert errors == []
    [statement] = specs["bare"].queries
    assert f"\nFROM {quoted}\n" in statement.sql
    assert f"\nFROM {quoted}\n" in statement.count_sql
    # The default target lands in the same data database.
    assert statement.sql.startswith("INSERT INTO `tenant_a`.`detection`\n")


def test_a_rule_files_bare_table_is_read_from_the_data_database(tmp_path: Path):
    rules_dir = tmp_path / "rules"
    _save_rule(rules_dir, source_table="cisco-ios")
    _write_hunt(tmp_path / "hunts", "bare")

    specs = load_specs(tmp_path / "hunts", **_runner_sources(rules_dir, "tenant_a"))

    [statement] = specs["bare"].queries
    assert "\nFROM `tenant_a`.`cisco-ios`\n" in statement.sql


def test_without_a_data_database_an_unqualified_source_still_has_none(tmp_path: Path):
    _save_rule(tmp_path)

    statements, errors = _errors(
        lambda: compile_hunt_queries(
            {"rules": ["certutil"], "global_source_table_name": "cisco-ios"},
            "h",
            rules_dir=tmp_path,
            default_target="dfe.detection",
        )
    )

    assert statements == []
    assert errors == ["hunt h: rule 'certutil' resolves to no db.table source"]
