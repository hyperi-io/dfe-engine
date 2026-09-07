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


def test_the_schema_cli_does_not_import_the_full_settings_loader():
    """A future edit that reaches for load_settings reintroduces the crash."""
    from dfe_engine.schema import cli

    assert not hasattr(cli, "load_settings"), (
        "dfe_engine.schema.cli imported load_settings again -- that reinstates "
        "the API posture validators in a tool with no API surface"
    )
