#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_hunt_customers.py
#  Purpose:      A compiled hunt reads only the tenant ids its customers resolved to
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A hunt's ``customers`` are org names, and it reads only the tenant ids they resolved to.

The compiled statement used to read every org in the source table, so a hunt for
one customer wrote every other tenant's matches into its results. The engine
stores each customer's tenant ids beside the names when it saves the hunt, because
the runner cannot read the org registry. A hunt naming no customer, or holding one
with no stored tenant ids, compiles to nothing and its fire fails saying why.
"""

from pathlib import Path

import pytest
from common.hunt_files import resolved_customers

from dfe_engine.hunt_runner import HuntWorker, load_specs
from dfe_engine.hunt_runner.rule_compiler import compile_hunt_queries
from dfe_engine.hunt_runner.runner import failure_reason
from dfe_engine.hunts.rule_model import Rule
from dfe_engine.hunts.rule_registry import RuleRegistry
from dfe_engine.yaml_utils import yaml_dump_string

# The hunt YAML key the engine writes and the runner reads, pinned as the file contract.
CUSTOMER_TENANT_IDS = "customer_tenant_ids"


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


class TestTheStatementIsHeldToTheResolvedTenantIds:
    def test_the_insert_and_its_count_read_only_the_stored_tenant_ids(self, rules_dir: Path):
        definition = _hunt(
            customers=["acme", "globex"],
            customer_tenant_ids={"acme": ["acme-1", "acme-2"], "globex": ["globex"]},
        )

        (statement,) = compile_hunt_queries(definition, "windows_hunt", rules_dir=rules_dir)

        held = "_org_id IN ('acme-1', 'acme-2', 'globex')"
        assert held in statement.sql
        assert held in statement.count_sql

    def test_an_org_name_is_never_read_as_a_tenant_id(self, rules_dir: Path):
        """An org declaring tenant ids is read by those, not by its name."""
        definition = _hunt(customers=["acme"], customer_tenant_ids={"acme": ["acme-1"]})

        (statement,) = compile_hunt_queries(definition, "windows_hunt", rules_dir=rules_dir)

        assert "_org_id IN ('acme-1')" in statement.sql
        assert "'acme'" not in statement.sql

    def test_the_org_condition_is_its_own_conjunct_beside_the_rule(self, rules_dir: Path):
        (statement,) = compile_hunt_queries(
            _hunt(**resolved_customers("acme")), "windows_hunt", rules_dir=rules_dir
        )

        assert (
            "WHERE {window} AND _org_id IN ('acme') AND (\nprocess_name = 'certutil.exe'\n    )"
            in statement.sql
        )

    def test_a_tenant_id_is_quoted_as_a_literal(self, rules_dir: Path):
        definition = _hunt(customers=["acme"], customer_tenant_ids={"acme": ["o'brien\\"]})

        (statement,) = compile_hunt_queries(definition, "windows_hunt", rules_dir=rules_dir)

        assert "_org_id IN ('o''brien\\\\')" in statement.sql

    @pytest.mark.parametrize(
        "customers",
        [None, [], [""], ["  "], "acme", [7]],
        ids=["absent", "empty", "blank", "whitespace", "not-a-list", "not-a-string"],
    )
    def test_a_hunt_naming_no_customer_compiles_to_nothing(self, rules_dir: Path, customers):
        definition = _hunt() if customers is None else _hunt(customers=customers)

        assert compile_hunt_queries(definition, "windows_hunt", rules_dir=rules_dir) == []

    @pytest.mark.parametrize(
        "stored",
        [None, {}, {"acme": []}, {"acme": "acme"}, {"acme": [7, " "]}, ["acme"]],
        ids=["absent", "no-entry", "empty", "not-a-list", "no-string", "not-a-mapping"],
    )
    def test_a_customer_with_no_stored_tenant_ids_compiles_to_nothing(
        self, rules_dir: Path, stored
    ):
        """A hunt written straight into the deploy repo names orgs the engine never resolved."""
        fields = {"customers": ["acme"]}
        if stored is not None:
            fields[CUSTOMER_TENANT_IDS] = stored

        assert compile_hunt_queries(_hunt(**fields), "windows_hunt", rules_dir=rules_dir) == []

    def test_one_unresolved_customer_stops_the_resolved_ones_too(self, rules_dir: Path):
        """Running the others would advance the watermark past the unresolved one's window."""
        definition = _hunt(customers=["acme", "globex"], customer_tenant_ids={"acme": ["acme"]})

        assert compile_hunt_queries(definition, "windows_hunt", rules_dir=rules_dir) == []

    def test_a_rule_closing_its_own_bracket_compiles_to_nothing(self, tmp_path: Path):
        """A condition that closes the bracket it runs in could OR its way past the org."""
        _save_rule(tmp_path / "rules", "certutil", "process_name = 'x'\n) OR (1 = 1")

        compiled = compile_hunt_queries(
            _hunt(**resolved_customers("acme")), "windows_hunt", rules_dir=tmp_path / "rules"
        )

        assert compiled == []

    @pytest.mark.parametrize(
        "where",
        [
            "process_name IN (SELECT process_name FROM dfe.main WHERE _org_id = 'globex')",
            "_source GLOBAL NOT IN (SELECT ioc FROM threat.iocs)",
            "EXISTS (SELECT 1 FROM dfe.main WHERE _org_id = 'globex')",
            "process_name = (SELECT any(process_name) FROM dfe.main)",
            "process_name IN dfe.main",
            "process_name IN (dfe.main)",
            "process_name NOT IN ((`main`))",
        ],
        ids=[
            "in-subquery",
            "global-not-in-subquery",
            "exists",
            "scalar-subquery",
            "in-table",
            "in-bracketed-table",
            "not-in-quoted-table",
        ],
    )
    def test_a_rule_reading_another_table_compiles_to_nothing(self, tmp_path: Path, where):
        """The runner's user is not pinned to an org, so another table answers from every org."""
        _save_rule(tmp_path / "rules", "certutil", where)

        compiled = compile_hunt_queries(
            _hunt(**resolved_customers("acme")), "windows_hunt", rules_dir=tmp_path / "rules"
        )

        assert compiled == []

    @pytest.mark.parametrize(
        "where",
        [
            "process_name IN (SELECT ioc FROM threat.iocs)",
            "process_name NOT IN (SELECT ioc FROM threat.iocs WHERE kind = 'process')",
            "process_name IN threat.iocs",
            "process_name IN (threat.iocs)",
        ],
        ids=["in-subquery", "not-in-filtered-subquery", "in-table", "in-bracketed-table"],
    )
    def test_a_rule_reading_a_listed_lookup_database_compiles(self, tmp_path: Path, where):
        _save_rule(tmp_path / "rules", "certutil", where)

        (statement,) = compile_hunt_queries(
            _hunt(**resolved_customers("acme")),
            "windows_hunt",
            rules_dir=tmp_path / "rules",
            lookup_databases=("threat",),
        )

        assert where in statement.sql
        assert "_org_id IN ('acme')" in statement.sql

    def test_a_rule_reading_an_unlisted_database_compiles_to_nothing(self, tmp_path: Path):
        _save_rule(tmp_path / "rules", "certutil", "process_name IN (SELECT ioc FROM intel.iocs)")

        compiled = compile_hunt_queries(
            _hunt(**resolved_customers("acme")),
            "windows_hunt",
            rules_dir=tmp_path / "rules",
            lookup_databases=("threat",),
        )

        assert compiled == []

    def test_a_bracket_inside_a_string_or_a_comment_is_not_counted(self, tmp_path: Path):
        _save_rule(tmp_path / "rules", "certutil", "msg = ')' -- (unbalanced in a comment")

        compiled = compile_hunt_queries(
            _hunt(**resolved_customers("acme")), "windows_hunt", rules_dir=tmp_path / "rules"
        )

        assert len(compiled) == 1


class _Untouchable:
    """Anything the worker must not reach for a hunt it refuses."""

    def __getattr__(self, name: str):
        raise AssertionError(f"worker reached {name} for a hunt it refuses")


def _refusal(tmp_path: Path, rules_dir: Path, **fields) -> BaseException:
    """Load the hunt as the runner does and fire it with nothing it may touch."""
    hunts = tmp_path / "hunts"
    hunts.mkdir()
    (hunts / "orphan.yaml").write_text(
        yaml_dump_string({**_hunt(**fields), "cron": "* * * * *"}), encoding="utf-8"
    )
    spec = load_specs(hunts, rules_dir=rules_dir)["orphan"]
    worker = HuntWorker(_Untouchable(), _Untouchable())
    with pytest.raises(Exception) as caught:
        worker.run(spec, scheduled_start=600)
    return caught.value


class TestARefusedHuntSaysSo:
    def test_naming_no_customer_fails_as_no_customers_before_touching_clickhouse(
        self, rules_dir: Path, tmp_path: Path
    ):
        refused = _refusal(tmp_path, rules_dir)

        assert failure_reason(refused) == "no_customers"
        assert "customer" in str(refused)

    def test_an_unresolved_customer_fails_as_unknown_customers_naming_it(
        self, rules_dir: Path, tmp_path: Path
    ):
        fields = {"customers": ["acme", "ghost"], CUSTOMER_TENANT_IDS: {"acme": ["acme"]}}

        refused = _refusal(tmp_path, rules_dir, **fields)

        assert failure_reason(refused) == "unknown_customers"
        assert "(ghost)" in str(refused)
