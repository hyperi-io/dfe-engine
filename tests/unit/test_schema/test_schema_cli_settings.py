#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_schema_cli_settings.py
#  Purpose:      Pin the schema tooling to a NARROW settings load, so an API
#                posture validator cannot fail a job that never serves a request
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The schema CLI must not load the API's settings.

``DFESettings`` refuses a production posture that leaves auth off or keeps the
dev jwt_secret. Those validators are right for the service and fatal for
``dfe-schema``, which connects to ClickHouse and applies DDL with no API surface
to secure. Loading the full model made an unset ``DFE_API_JWT_SECRET`` fail the
deploy's schema Job -- observed on a real cluster, where the Job crashed with a
pydantic ValidationError before it ever reached ClickHouse.

The fix narrows the load rather than relaxing the validators, and these tests
pin both halves of that: the narrow loader survives the posture the full model
rejects, and the full model still rejects it.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import dfe_schemas
import pytest
from pydantic import ValidationError

from dfe_engine.schema.core_schema import CoreSchemaTargets, core_table_specs
from dfe_engine.settings import (
    ClickHouseSettings,
    load_clickhouse_settings,
    load_settings,
)

# The posture a k8s Job inherits by default: env unset (so `production`), auth
# on, and no jwt_secret supplied because the Job has no API to secure.
_PRODUCTION_NO_SECRET = {
    "DFE_ENV": "production",
    "DFE_AUTH_ENABLED": "true",
}


@pytest.fixture
def production_without_secret(monkeypatch):
    for key, value in _PRODUCTION_NO_SECRET.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("DFE_API_JWT_SECRET", raising=False)
    # load_settings() re-reads cwd/.env; a developer secret must not sneak back in.
    monkeypatch.setattr("dfe_engine.env_files.load_env_files", lambda: None)


def test_schema_loader_survives_a_production_posture_with_no_jwt_secret(
    production_without_secret,
):
    """The regression: this is exactly what failed the deploy's schema Job."""
    ch = load_clickhouse_settings()
    assert isinstance(ch, ClickHouseSettings)
    assert ch.effective_data_database


def test_the_full_model_still_rejects_that_posture(production_without_secret):
    """Narrowing the load must not have weakened the validator it dodges."""
    with pytest.raises(ValidationError, match="jwt_secret"):
        load_settings()


def test_the_narrow_loader_honours_env_overrides(production_without_secret, monkeypatch):
    """It reads the same cascade, or the Job would connect to the wrong cluster."""
    monkeypatch.setenv("DFE_CLICKHOUSE_HOST", "ch.example.test")
    monkeypatch.setenv("DFE_CLICKHOUSE_DATA_DATABASE", "not_the_default")

    ch = load_clickhouse_settings()

    assert ch.host == "ch.example.test"
    assert ch.effective_data_database == "not_the_default"


def test_targets_read_the_same_values_from_either_shape():
    """from_clickhouse and from_settings must not drift apart."""
    ch = ClickHouseSettings()

    class _Wrapper:
        clickhouse = ch

    assert CoreSchemaTargets.from_clickhouse(ch) == CoreSchemaTargets.from_settings(_Wrapper())


def test_the_landing_table_setting_reaches_the_bootstrapped_table(monkeypatch):
    """The bootstrap creates the table the query paths read, not a hardcoded name."""
    monkeypatch.setenv("DFE_CLICKHOUSE_LANDING_TABLE", "parked")

    targets = CoreSchemaTargets.from_clickhouse(load_clickhouse_settings())

    assert targets.landing_table == "parked"
    assert core_table_specs(targets)[0].name == "parked"


def test_the_default_ttl_env_reaches_the_targets(monkeypatch):
    monkeypatch.setenv("DFE_CLICKHOUSE_DEFAULT_TTL_DAYS", "30")
    assert CoreSchemaTargets.from_clickhouse(load_clickhouse_settings()).default_ttl_days == 30

    # 0 is carried through, so the tables that follow it have their TTL removed.
    monkeypatch.setenv("DFE_CLICKHOUSE_DEFAULT_TTL_DAYS", "0")
    assert CoreSchemaTargets.from_clickhouse(load_clickhouse_settings()).default_ttl_days == 0


def test_a_zero_default_declares_no_ttl_on_the_time_series_tables_only(monkeypatch):
    monkeypatch.setenv("DFE_CLICKHOUSE_DEFAULT_TTL_DAYS", "0")

    targets = CoreSchemaTargets.from_clickhouse(load_clickhouse_settings())
    specs = {spec.name: spec for spec in core_table_specs(targets)}

    assert specs[targets.landing_table].config.ttl_days == 0
    assert specs["detection"].config.ttl_days == 0
    assert specs["detection_checkpoint"].config.ttl_days == 30


def test_the_default_ttl_reaches_the_time_series_tables_that_declare_none(monkeypatch):
    """The shipped landing, detection and OTel tables inherit it; the state tables never do."""
    monkeypatch.setenv("DFE_CLICKHOUSE_DEFAULT_TTL_DAYS", "45")

    targets = CoreSchemaTargets.from_clickhouse(load_clickhouse_settings())
    specs = {spec.name: spec for spec in core_table_specs(targets)}
    otel = {name: spec for name, spec in specs.items() if name.startswith("otel_")}

    assert targets.default_ttl_days == 45
    assert specs[targets.landing_table].config.ttl_days == 45
    assert specs["detection"].config.ttl_days == 45
    assert otel
    assert {spec.config.ttl_days for spec in otel.values()} == {45}

    # Internal and coordination tables are state: the default never touches them.
    without = {
        spec.name: spec for spec in core_table_specs(replace(targets, default_ttl_days=None))
    }
    state = set(specs) - set(otel) - {targets.landing_table, "detection"}
    assert state
    assert all(specs[name].config.ttl_days == without[name].config.ttl_days for name in state)
    assert specs["detection_checkpoint"].config.ttl_days == 30


def _schemas_tree_with_declared_ttl(root: Path) -> Path:
    """A copy of the installed dfe-schemas tree that declares ttl_days on two time-series tables."""
    tree = root / "schemas"
    shutil.copytree(Path(dfe_schemas.__file__).parent / "data", tree)
    for relative in ("core/default.yaml", "otel/logs.yaml"):
        path = tree / "tables" / relative
        lines = path.read_text(encoding="utf-8").splitlines()
        anchor = next(i for i, line in enumerate(lines) if line.strip().startswith("ttl_columns:"))
        indent = " " * (len(lines[anchor]) - len(lines[anchor].lstrip()))
        lines.insert(anchor, f"{indent}ttl_days: 30")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return tree


def test_a_declared_ttl_wins_over_the_deployment_default(tmp_path, monkeypatch):
    """A table that declares ttl_days keeps it, whatever the deployment default says."""
    monkeypatch.setenv("DFE_SCHEMAS_DIR", str(_schemas_tree_with_declared_ttl(tmp_path)))
    monkeypatch.setenv("DFE_CLICKHOUSE_DEFAULT_TTL_DAYS", "45")

    targets = CoreSchemaTargets.from_clickhouse(load_clickhouse_settings())
    specs = {spec.name: spec for spec in core_table_specs(targets)}

    assert specs[targets.landing_table].config.ttl_days == 30
    assert specs["otel_logs"].config.ttl_days == 30
    # The tables left alone still inherit, so the fixture proved a difference.
    assert specs["detection"].config.ttl_days == 45
    assert specs["otel_traces"].config.ttl_days == 45


def test_the_schema_cli_does_not_import_the_full_settings_loader():
    """A future edit that reaches for load_settings reintroduces the crash."""
    from dfe_engine.schema import cli

    assert not hasattr(cli, "load_settings"), (
        "dfe_engine.schema.cli imported load_settings again -- that reinstates "
        "the API posture validators in a tool with no API surface"
    )
