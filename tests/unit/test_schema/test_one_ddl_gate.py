#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_one_ddl_gate.py
#  Purpose:      Every DDL the engine renders takes the one replicated shape
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The CI gate on the one DDL shape, over everything the engine renders without a server.

A plain MergeTree on a cluster is accepted and then lands on one replica, where
every query succeeds and the answer depends which node answered. ``ON CLUSTER``
on a table inside the Replicated database is refused outright (code 80). So every
table the engine renders must be ``Replicated*``, and only the self-hosted CREATE
DATABASE may fan out. dfe-schemas' ``ddl_violations`` is the check, and this
feeds it every render path the engine has: the manifest on both platforms, the
reference tables, every engine variant a source may pick, and the ALTER and view
forms a deploy runs.
"""

import pytest
from dfe_schemas.clickhouse import Platform
from dfe_schemas.render import ddl_violations

from dfe_engine.schema.ddl_writer import DDLFileWriter
from dfe_engine.schema.plan import build_plan
from dfe_engine.schema.schema_ddl import DDLConfig, DDLGenerator
from dfe_engine.schema.schema_loader import SchemaLoader
from dfe_engine.settings import APISettings, DFESettings
from dfe_engine.source.engine_registry import EngineRegistry
from dfe_engine.source.type_registry import TypeRegistry

PLATFORMS = list(Platform)


def _leaves(tree: dict) -> list[str]:
    """Every SQL string in DDLFileWriter's nested output."""
    found: list[str] = []
    for value in tree.values():
        found.extend(_leaves(value) if isinstance(value, dict) else [value])
    return found


def _engines() -> list[str]:
    """Every engine a source may select, with arguments where the variant takes them."""
    engines = []
    for option in EngineRegistry.default().options:
        if option.arguments == "required":
            engines.append(f"{option.name}(_timestamp_load)")
        else:
            engines.append(option.name)
    return engines


def _problems(statements: list[str]) -> list[str]:
    return [problem for statement in statements for problem in ddl_violations(statement)]


@pytest.mark.parametrize("platform", PLATFORMS)
def test_every_manifest_object_takes_the_one_shape(tmp_path, platform: Platform):
    settings = DFESettings(
        config_dir=str(tmp_path),
        api=APISettings(jwt_secret="test-secret-key-for-unit-tests-hmac32"),
    )
    plan = build_plan(settings=settings, platform=platform)

    statements = [statement for obj in plan.tables() for statement in obj.statements]
    assert sum(obj.kind == "table" for obj in plan.tables()) >= 20
    assert _problems(statements) == []


@pytest.mark.parametrize("platform", PLATFORMS)
def test_every_reference_table_takes_the_one_shape(platform: Platform):
    statements = _leaves(DDLFileWriter(platform=platform, database="dfe").generate_all())

    assert len(statements) >= 4
    assert _problems(statements) == []


@pytest.mark.parametrize("engine", _engines())
def test_every_engine_a_source_may_pick_renders_replicated(engine: str):
    columns = SchemaLoader.load_profile("timeseries")
    gen = DDLGenerator(TypeRegistry.default())

    ddl = gen.generate_create_table("probe", columns, DDLConfig(db="dfe", engine=engine))

    assert _problems([ddl]) == []


def test_every_alter_and_view_form_a_deploy_runs_carries_no_on_cluster():
    columns = SchemaLoader.load_profile("timeseries")
    gen = DDLGenerator(TypeRegistry.default())
    cfg = DDLConfig(db="dfe")
    statements = [
        gen.generate_alter_add_column("probe", columns[0], cfg),
        gen.generate_alter_modify_column("probe", columns[0], cfg),
        gen.generate_view("probe", {"event.code": "_org_id"}, "ecs", cfg),
    ]
    for column in columns:
        statements.extend(gen.generate_alter_add_indexes("probe", column, cfg))

    assert _problems(statements) == []


def test_the_gate_is_not_vacuous():
    """A plain engine and a fanned-out ALTER are both caught."""
    assert ddl_violations("CREATE TABLE d.t (x UInt8)\nENGINE = MergeTree()\nORDER BY x")
    assert ddl_violations("ALTER TABLE d.t ON CLUSTER '{cluster}' ADD COLUMN y String")
