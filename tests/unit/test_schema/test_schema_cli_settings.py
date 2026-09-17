#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_schema_cli_settings.py
#  Purpose:      Pin the narrow settings load and the deployment's retention default
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The schema tooling's settings load, and where the deployment default TTL lands.

``DFESettings`` refuses a production posture that leaves auth off or keeps the
dev jwt_secret. Those validators are right for the service and fatal for a
one-shot process that connects to ClickHouse and applies DDL with no API surface
to secure: loading the full model made an unset ``DFE_API_JWT_SECRET`` fail a
deploy's schema step on a real cluster, with a pydantic ValidationError before it
ever reached ClickHouse. The narrow loader is the answer, and the first two tests
pin both halves of it.

The rest pin where ``DFE_CLICKHOUSE_DEFAULT_TTL_DAYS`` reaches: the time-series
tables that declare no retention of their own, and nothing else. A table that
declares its own ``ttl_days`` keeps it whatever the deployment default says.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import dfe_schemas
import pytest
from pydantic import ValidationError

from dfe_engine.schema.core_schema import CoreSchemaTargets
from dfe_engine.schema.plan import build_plan
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


def _ttls(monkeypatch) -> dict[str, int | None]:
    """Every rendered table's TTL in days, by name, for the current environment."""
    plan = build_plan(settings=load_settings())
    found: dict[str, int | None] = {}
    for rendered in plan.tables():
        if rendered.kind != "table":
            continue
        statement = rendered.statements[0]
        _, sep, tail = statement.partition("\nTTL ")
        if not sep:
            found[rendered.name] = None
            continue
        clause = tail.split("\nSETTINGS", 1)[0]
        found[rendered.name] = int(clause.split("INTERVAL ", 1)[1].split(" DAY", 1)[0])
    return found


def test_schema_loader_survives_a_production_posture_with_no_jwt_secret(
    production_without_secret,
):
    """The regression: this is exactly what failed the deploy's schema step."""
    ch = load_clickhouse_settings()
    assert isinstance(ch, ClickHouseSettings)
    assert ch.effective_data_database


def test_the_full_model_still_rejects_that_posture(production_without_secret):
    """Narrowing the load must not have weakened the validator it dodges."""
    with pytest.raises(ValidationError, match="jwt_secret"):
        load_settings()


def test_the_narrow_loader_honours_env_overrides(production_without_secret, monkeypatch):
    """It reads the same cascade, or the tooling would connect to the wrong cluster."""
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


def test_the_default_ttl_env_reaches_the_targets(monkeypatch):
    monkeypatch.setenv("DFE_CLICKHOUSE_DEFAULT_TTL_DAYS", "30")
    assert CoreSchemaTargets.from_clickhouse(load_clickhouse_settings()).default_ttl_days == 30

    # 0 is carried through, so the tables that follow it have their TTL removed.
    monkeypatch.setenv("DFE_CLICKHOUSE_DEFAULT_TTL_DAYS", "0")
    assert CoreSchemaTargets.from_clickhouse(load_clickhouse_settings()).default_ttl_days == 0


def test_a_zero_default_declares_no_ttl_on_the_time_series_tables_only(monkeypatch):
    monkeypatch.setenv("DFE_CLICKHOUSE_DEFAULT_TTL_DAYS", "0")

    ttls = _ttls(monkeypatch)

    assert ttls["main"] is None
    assert ttls["detection"] is None
    # A declared retention is not a default, so it survives the dial being off.
    assert ttls["query_log_archive"] == 30


def test_the_default_ttl_reaches_the_time_series_tables_that_declare_none(monkeypatch):
    """The shipped landing, detection and OTel tables inherit it; the state tables never do."""
    monkeypatch.setenv("DFE_CLICKHOUSE_DEFAULT_TTL_DAYS", "45")

    ttls = _ttls(monkeypatch)
    otel = {name: days for name, days in ttls.items() if name.startswith("otel_")}

    assert ttls["main"] == 45
    assert ttls["detection"] == 45
    assert otel
    assert set(otel.values()) == {45}

    # Coordination tables are state: the default never touches them.
    assert ttls["hunt_lease"] is None
    assert ttls["hunt_state"] is None
    assert ttls["schema_migrations"] is None


def _schemas_tree_with_declared_ttl(root: Path) -> Path:
    """A copy of the installed tree that declares ttl_days on two time-series tables."""
    tree = root / "schemas"
    shutil.copytree(Path(dfe_schemas.__file__).parent / "data", tree)
    for relative in ("core/main.yaml", "otel/logs.yaml"):
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

    ttls = _ttls(monkeypatch)

    assert ttls["main"] == 30
    assert ttls["otel_logs"] == 30
    # The tables left alone still inherit, so the fixture proved a difference.
    assert ttls["detection"] == 45
    assert ttls["otel_traces"] == 45
