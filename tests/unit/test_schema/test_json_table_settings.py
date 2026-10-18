#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_json_table_settings.py
#  Purpose:      A source table with a JSON column renders and reconciles the merge cap
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The CREATE carries dfe-schemas' JSON table settings, and the reconcile plans the rest.

The value is read from ``dfe_schemas.deploy_defaults`` and never restated here, so
changing it there is the only edit a new value needs.
"""

import pytest
from dfe_schemas.clickhouse import Platform
from dfe_schemas.deploy_defaults import JSON_TABLE_SETTINGS

from dfe_engine.schema.applier import settings_from_engine_full, settings_move
from dfe_engine.schema.schema_ddl import DDLConfig, DDLGenerator
from dfe_engine.schema.schema_loader import SchemaLoader
from dfe_engine.source.models import SchemaColumn
from dfe_engine.source.type_registry import TypeRegistry

MERGE_CAP = "merge_max_dynamic_subcolumns_in_wide_part"
WRITE_MARKS = "write_marks_for_substreams_in_compact_parts"

# engine_full as ClickHouse 26.3.42.3 reports a capped JSON table.
_ENGINE_FULL = (
    "MergeTree PARTITION BY toYYYYMMDD(t) ORDER BY t TTL t + toIntervalDay(90) WHERE t >= 0 "
    "SETTINGS index_granularity = 2048, ttl_only_drop_parts = 1, "
    "merge_max_dynamic_subcolumns_in_wide_part = 64"
)


@pytest.fixture
def gen() -> DDLGenerator:
    return DDLGenerator(TypeRegistry.default())


def _settings_clause(ddl: str) -> str:
    _, sep, tail = ddl.partition("\nSETTINGS\n")
    assert sep, ddl
    return tail.split("\nCOMMENT ", 1)[0]


def _plain_columns() -> list[SchemaColumn]:
    return [
        SchemaColumn(name="_timestamp_load", type="timestamp", order=0),
        SchemaColumn(name="message", type="text"),
    ]


def test_a_json_column_puts_the_merge_cap_on_the_create(gen: DDLGenerator):
    """Uncapped, a merge over map-shaped JSON keys writes a stream per key and runs out of memory."""
    columns = [*_plain_columns(), SchemaColumn(name="_json", type="json", max_dynamic_paths=2048)]

    clause = _settings_clause(gen.generate_create_table("t", columns, DDLConfig()))

    assert f"{MERGE_CAP} = " in clause
    for name, value in JSON_TABLE_SETTINGS.items():
        assert f"{name} = {value}" in clause


def test_a_table_without_a_json_column_carries_no_merge_cap(gen: DDLGenerator):
    ddl = gen.generate_create_table("t", _plain_columns(), DDLConfig())

    assert MERGE_CAP not in ddl


@pytest.mark.parametrize("profile", ["timeseries", "minimal"])
def test_every_source_landing_table_carries_the_cap(gen: DDLGenerator, profile: str):
    """Both shipped headers carry _json, so every source landing table takes the cap."""
    ddl = gen.generate_create_table("s3pypi", SchemaLoader.load_profile(profile), DDLConfig())

    assert f"{MERGE_CAP} = {JSON_TABLE_SETTINGS[MERGE_CAP]}" in _settings_clause(ddl)


def test_the_cap_sits_beside_the_ttl_settings_not_instead_of_them(gen: DDLGenerator):
    columns = [*_plain_columns(), SchemaColumn(name="_json", type="json")]

    clause = _settings_clause(gen.generate_create_table("t", columns, DDLConfig(ttl_days=30)))

    assert "index_granularity = 2048" in clause
    assert "ttl_only_drop_parts = 1" in clause
    assert MERGE_CAP in clause


def test_engine_full_settings_are_read_by_name():
    assert settings_from_engine_full(_ENGINE_FULL) == {
        "index_granularity": "2048",
        "ttl_only_drop_parts": "1",
        MERGE_CAP: "64",
    }


def test_a_quoted_setting_value_keeps_its_comma():
    full = "MergeTree ORDER BY t SETTINGS storage_policy = 'a, b', index_granularity = 8192"

    assert settings_from_engine_full(full) == {
        "storage_policy": "'a, b'",
        "index_granularity": "8192",
    }


def test_engine_full_without_settings_reads_as_none():
    assert settings_from_engine_full("MergeTree ORDER BY t") == {}
    assert settings_from_engine_full("") == {}


def test_a_missing_setting_is_set_with_one_alter():
    move = settings_move(
        target="`dfe`.`s3pypi`",
        wanted={MERGE_CAP: 64},
        live={"index_granularity": "2048"},
    )

    assert move.statements == (f"ALTER TABLE `dfe`.`s3pypi` MODIFY SETTING {MERGE_CAP} = 64",)
    assert move.assignments == (f"{MERGE_CAP} = 64",)


def test_a_different_value_is_moved_to_the_declared_one():
    move = settings_move(target="`dfe`.`s3pypi`", wanted={MERGE_CAP: 64}, live={MERGE_CAP: "128"})

    assert move.statements[0].endswith(f"MODIFY SETTING {MERGE_CAP} = 64")


def test_a_setting_already_in_place_issues_nothing():
    move = settings_move(target="`dfe`.`s3pypi`", wanted={MERGE_CAP: 64}, live={MERGE_CAP: "64"})

    assert move.statements == ()
    assert move.assignments == ()


def test_no_alter_carries_on_cluster():
    """The Replicated database replicates the ALTER, and refuses ON CLUSTER inside it."""
    move = settings_move(target="`dfe`.`main`", wanted={MERGE_CAP: 64}, live={})

    assert move.statements == (f"ALTER TABLE `dfe`.`main` MODIFY SETTING {MERGE_CAP} = 64",)


def test_a_platform_only_setting_the_platform_omits_is_reset_in_its_own_alter():
    """ClickHouse refuses MODIFY SETTING and RESET SETTING in one ALTER (code 62)."""
    move = settings_move(
        target="`dfe`.`main`",
        wanted={MERGE_CAP: 32},
        live={MERGE_CAP: "64", WRITE_MARKS: "0"},
    )

    assert move.statements == (
        f"ALTER TABLE `dfe`.`main` MODIFY SETTING {MERGE_CAP} = 32",
        f"ALTER TABLE `dfe`.`main` RESET SETTING {WRITE_MARKS}",
    )
    assert move.assignments == (f"{MERGE_CAP} = 32", f"reset {WRITE_MARKS}")


@pytest.mark.parametrize(
    ("platform", "carries"), [(Platform.SELF_HOSTED, True), (Platform.CLOUD, False)]
)
def test_write_marks_rides_a_json_create_on_self_hosted_only(platform: Platform, carries: bool):
    gen = DDLGenerator(TypeRegistry.default(), platform=platform)
    columns = [*_plain_columns(), SchemaColumn(name="_json", type="json")]

    clause = _settings_clause(gen.generate_create_table("t", columns, DDLConfig()))

    assert (f"{WRITE_MARKS} = 0" in clause) is carries
    assert MERGE_CAP in clause
