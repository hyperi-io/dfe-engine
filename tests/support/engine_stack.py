#  Project:      dfe-engine
#  File:         tests/support/engine_stack.py
#  Purpose:      Settings and source deploys for integration tests that drive the engine against a real ClickHouse
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Build the engine's settings for the harness's ClickHouse and deploy a source through the engine API.

The ``stack`` fixture in ``tests/integration/conftest.py`` builds the engine API from :func:`api_settings`.
"""

import re
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dfe_schemas

from dfe_engine.schema.lock import SchemaLock
from dfe_engine.schema.plan import LOCK_ID, render_one
from dfe_engine.schema.retention import CONVERGE_ATTRIBUTE
from dfe_engine.settings import (
    APISettings,
    AuthSettings,
    ClickHouseResilienceSettings,
    ClickHouseSettings,
    DFESettings,
    HuntsSettings,
    KafkaSettings,
    LocalAuthSettings,
    SchemasSettings,
    SecretsSettings,
    ServicesSettings,
    SourceSettings,
    load_settings,
)

# The local admin's password once the stack has moved it off the issued one.
ADMIN_PASSWORD = "test-admin-pw"


def api_settings(*, ch_params: dict[str, Any], database: str, tmp_path: Path) -> DFESettings:
    """Settings for the engine API on the harness's ClickHouse, with the schema bootstrap off and nothing read from .env."""
    for sub in ("sources", "services", "rules", "hunts", "auth", "secrets"):
        (tmp_path / sub).mkdir()
    # A copy of the shipped tree, so the build reads real definitions and nothing a test does can write into the installed package.
    schemas_dir = tmp_path / "schemas"
    shutil.copytree(Path(dfe_schemas.__file__).parent / "data", schemas_dir)
    return DFESettings(
        api=APISettings(jwt_secret="test-secret-key-for-unit-tests-hmac32"),
        auth=AuthSettings(
            auth_dir=str(tmp_path / "auth"),
            enabled=True,
            local=LocalAuthSettings(admin_password=ADMIN_PASSWORD),
        ),
        clickhouse=ClickHouseSettings(
            bootstrap_tables=False,
            data_database=database,
            default_ttl_days=90,
            host=ch_params["host"],
            password=ch_params["password"],
            port=ch_params["port"],
            resilience=ClickHouseResilienceSettings(budget_seconds=5.0),
            secure=False,
            username=ch_params["username"],
        ),
        config_dir=str(tmp_path),
        hunts=HuntsSettings(hunt_dir=str(tmp_path / "hunts"), rules_dir=str(tmp_path / "rules")),
        kafka=KafkaSettings(ensure_topics=False),
        schemas=SchemasSettings(schemas_dir=str(schemas_dir)),
        secrets=SecretsSettings(path=str(tmp_path / "secrets"), provider="file"),
        services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
        source=SourceSettings(sources_dir=str(tmp_path / "sources")),
    )


def bootstrap_settings(
    *, bootstrap_tables: bool = True, ch_params: dict[str, Any], database: str
) -> DFESettings:
    """Settings aimed at the harness's ClickHouse, never at whatever .env names."""
    settings = load_settings()
    settings.clickhouse.host = ch_params["host"]
    settings.clickhouse.port = ch_params["port"]
    settings.clickhouse.username = ch_params["username"]
    settings.clickhouse.password = ch_params["password"]
    settings.clickhouse.secure = ch_params["secure"]
    settings.clickhouse.data_database = database
    settings.clickhouse.bootstrap_tables = bootstrap_tables
    # These assert ClickHouse objects. The topic step would reach whatever broker .env names.
    settings.kafka.bootstrap_topics = False
    return settings


_TTL_DAYS = re.compile(r"toIntervalDay\((\d+)\)")


def live_ttl_days(ch_client: Any, database: str, table: str) -> int | None:
    """The whole-day TTL on ``database.table`` as the server reports it, None for a table with none."""
    rows = ch_client.query(
        "SELECT engine_full FROM system.tables WHERE database = {db:String} AND name = {t:String}",
        parameters={"db": database, "t": table},
    ).result_rows
    assert rows, f"{database}.{table} does not exist"
    _, sep, clause = str(rows[0][0]).partition(" TTL ")
    match = _TTL_DAYS.search(clause) if sep else None
    return int(match.group(1)) if match else None


def hold_schema_lease(ch_client: Any, database: str, holder: str) -> SchemaLock:
    """Take the schema lease for *holder*, as an applier mid-apply does, and return the held lock."""
    lock = SchemaLock(
        ch_client,
        database=database,
        table=render_one(LOCK_ID, data_database=database).name,
        holder=holder,
        lease_seconds=300,
    )
    # A claim held up by a server stall is a missed attempt, so taking the lease here waits.
    assert lock.acquire(wait_seconds=10.0) is True
    return lock


def converged(stack: SimpleNamespace, timeout: float = 120.0) -> None:
    """Wait for the default-TTL pass the last change asked for, retries included, to end."""
    trigger = getattr(stack.app.state, CONVERGE_ATTRIBUTE)
    assert trigger.wait_idle(timeout), f"the default-TTL pass was still running after {timeout} s"


def default_header(stack: SimpleNamespace) -> dict[str, str]:
    """The common header a source inherits on *stack*, as the engine's own defaults report it."""
    resp = stack.client.get("/api/v1/system/defaults", headers=stack.headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return {
        "type": body["common_header_type"]["effective"],
        "version": body["common_header_version"]["effective"],
    }


def deploy_source(
    *,
    header: dict[str, str] | None = None,
    name: str,
    schema: dict[str, Any] | None = None,
    stack: SimpleNamespace,
) -> None:
    """Create a source through the engine API and deploy it, on the stack's default header and the syslog meta-schema unless told otherwise."""
    created = stack.client.post(
        "/api/v1/sources",
        headers=stack.headers,
        json={
            "display_name": name,
            "enabled": True,
            "header": header or default_header(stack),
            "match": {"field": "tags.collector.type", "value": name},
            "schema": schema or {"meta_schema": "meta/syslog"},
            "source": name,
        },
    )
    assert created.status_code in (200, 201), created.text
    deployed = stack.client.post(f"/api/v1/sources/{name}/deploy", headers=stack.headers)
    assert deployed.status_code == 200, deployed.text
