#  Project:      dfe-engine
#  File:         tests/support/engine_stack.py
#  Purpose:      Settings, source deploys and teardown for integration tests that drive the engine against a real ClickHouse
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Build the engine's settings for the harness's ClickHouse, deploy a source through the engine API and drop the row policies a test leaves behind.

The ``stack`` fixture in ``tests/integration/conftest.py`` builds the engine API from :func:`api_settings`.
"""

import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dfe_schemas

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
    # A .env may rename the landing table, a name the manifest the bootstrap applies does not follow.
    settings.clickhouse.landing_table = ClickHouseSettings.model_fields["landing_table"].default
    # These assert ClickHouse objects; the topic step would reach whatever broker .env names.
    settings.kafka.bootstrap_topics = False
    return settings


def deploy_source(
    *,
    header: dict[str, str] | None = None,
    name: str,
    schema: dict[str, Any] | None = None,
    stack: SimpleNamespace,
) -> None:
    """Create a source through the engine API and deploy it, on the timeseries profile and the syslog meta-schema unless told otherwise."""
    created = stack.client.post(
        "/api/v1/sources",
        headers=stack.headers,
        json={
            "display_name": name,
            "enabled": True,
            "header": header or {"type": "timeseries", "version": "1.0.0"},
            "match": {"field": "tags.collector.type", "value": name},
            "schema": schema or {"meta_schema": "meta/syslog"},
            "source": name,
        },
    )
    assert created.status_code in (200, 201), created.text
    deployed = stack.client.post(f"/api/v1/sources/{name}/deploy", headers=stack.headers)
    assert deployed.status_code == 200, deployed.text


def drop_row_policies(*, client: Any, database: str) -> None:
    """Drop every row policy on a table in *database*, since dropping a table or its database leaves its policies behind."""
    rows = client.query(
        "SELECT short_name, table FROM system.row_policies WHERE database = {db:String}",
        parameters={"db": database},
    ).result_rows
    for name, table in rows:
        # Teardown carries on past a policy that is already gone.
        try:
            client.command(f"DROP ROW POLICY IF EXISTS `{name}` ON `{database}`.`{table}`")
        except Exception:
            pass
