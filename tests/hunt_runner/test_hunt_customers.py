#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_hunt_customers.py
#  Purpose:      A compiled hunt reads only the orgs its customers name
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A hunt's ``customers`` are the ``_org_id`` values it may read, and nothing else.

The compiled statement used to read every org in the source table, so a hunt for
one customer wrote every other tenant's matches into its results. A hunt naming no
customer reads no org: it compiles to nothing, and its fire fails saying why.
"""

from pathlib import Path

import pytest

from dfe_engine.hunt_runner import HuntWorker, load_specs
from dfe_engine.hunt_runner.rule_compiler import compile_hunt_queries
from dfe_engine.hunt_runner.runner import failure_reason
from dfe_engine.hunts.rule_model import Rule
from dfe_engine.hunts.rule_registry import RuleRegistry
from dfe_engine.yaml_utils import yaml_dump_string


def _save_rule(rules_dir: Path, rule_id: str, where_clause: str) -> None:
    registry = RuleRegistry(rules_directory=rules_dir, writable=True, refresh_interval=0)
    try:
        registry.save(Rule(rule_id=rule_id, name=rule_id, where_clause=where_clause))
    finally:
        registry.close()


def _hunt(**fields) -> dict:
    return {
        "rules": [{"rule_name": "certutil"}],
        "global_source_table_name": "dfe.main",
        "global_target_table_name": "dfe.detection",
        **fields,
    }


@pytest.fixture
def rules_dir(tmp_path: Path) -> Path:
    _save_rule(tmp_path / "rules", "certutil", "process_name = 'certutil.exe'")
    return tmp_path / "rules"


class TestTheStatementIsHeldToTheCustomers:
    def test_the_insert_and_its_count_read_only_the_named_orgs(self, rules_dir: Path):
        (statement,) = compile_hunt_queries(
            _hunt(customers=["acme", "globex"]), "windows_hunt", rules_dir=rules_dir
        )

        held = "_org_id IN ('acme', 'globex')"
        assert held in statement.sql
        assert held in statement.count_sql

    def test_the_org_condition_is_its_own_conjunct_beside_the_rule(self, rules_dir: Path):
        (statement,) = compile_hunt_queries(
            _hunt(customers=["acme"]), "windows_hunt", rules_dir=rules_dir
        )

        assert (
            "WHERE {window} AND _org_id IN ('acme') AND (\nprocess_name = 'certutil.exe'\n    )"
            in statement.sql
        )

    def test_a_customer_is_quoted_as_a_literal(self, rules_dir: Path):
        (statement,) = compile_hunt_queries(
            _hunt(customers=["o'brien\\"]), "windows_hunt", rules_dir=rules_dir
        )

        assert "_org_id IN ('o''brien\\\\')" in statement.sql

    @pytest.mark.parametrize(
        "customers",
        [None, [], [""], ["  "], "acme", [7]],
        ids=["absent", "empty", "blank", "whitespace", "not-a-list", "not-a-string"],
    )
    def test_a_hunt_naming_no_customer_compiles_to_nothing(self, rules_dir: Path, customers):
        definition = _hunt() if customers is None else _hunt(customers=customers)

        assert compile_hunt_queries(definition, "windows_hunt", rules_dir=rules_dir) == []

    def test_a_rule_closing_its_own_bracket_compiles_to_nothing(self, tmp_path: Path):
        """A condition that closes the bracket it runs in could OR its way past the org."""
        _save_rule(tmp_path / "rules", "certutil", "process_name = 'x'\n) OR (1 = 1")

        compiled = compile_hunt_queries(
            _hunt(customers=["acme"]), "windows_hunt", rules_dir=tmp_path / "rules"
        )

        assert compiled == []

    def test_a_bracket_inside_a_string_or_a_comment_is_not_counted(self, tmp_path: Path):
        _save_rule(tmp_path / "rules", "certutil", "msg = ')' -- (unbalanced in a comment")

        compiled = compile_hunt_queries(
            _hunt(customers=["acme"]), "windows_hunt", rules_dir=tmp_path / "rules"
        )

        assert len(compiled) == 1


class _Untouchable:
    """Anything the worker must not reach for a hunt it refuses."""

    def __getattr__(self, name: str):
        raise AssertionError(f"worker reached {name} for a hunt naming no customer")


class TestAHuntNamingNoCustomerSaysSo:
    def test_its_fire_fails_as_no_customers_before_touching_clickhouse(
        self, rules_dir: Path, tmp_path: Path
    ):
        hunts = tmp_path / "hunts"
        hunts.mkdir()
        (hunts / "orphan.yaml").write_text(
            yaml_dump_string({**_hunt(), "cron": "* * * * *"}), encoding="utf-8"
        )
        specs = load_specs(hunts, rules_dir=rules_dir)

        worker = HuntWorker(_Untouchable(), _Untouchable())
        with pytest.raises(Exception) as caught:
            worker.run(specs["orphan"], scheduled_start=600)

        assert failure_reason(caught.value) == "no_customers"
        assert "customer" in str(caught.value)
