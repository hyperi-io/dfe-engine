#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_gitops_consolidation_task.py
#  Purpose:      The API lifespan runs the deploy-repo pack consolidation and stops it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The lifespan consolidates the deploy-repo clone's packs off the request path.

dulwich's own gc is switched off in the clone, so without this task nothing would
consolidate the packs every fetch adds. Real lifespan, local-only deploy repo.
"""

import secrets
from pathlib import Path

from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries
from dfe_engine.settings import (
    APISettings,
    AuthSettings,
    ClickHouseSettings,
    DFESettings,
    GitopsPackSettings,
    GitopsSettings,
    HuntsSettings,
    LocalAuthSettings,
    SchemasSettings,
    SecretsSettings,
    ServicesSettings,
    SourceSettings,
)


def _settings(tmp_path: Path) -> DFESettings:
    for sub in ("sources", "services", "rules", "hunts", "auth", "schemas", "secrets", "orgs"):
        (tmp_path / sub).mkdir()
    return DFESettings(
        env="dev",
        config_dir=str(tmp_path),
        clickhouse=ClickHouseSettings(bootstrap_tables=False),
        source=SourceSettings(sources_dir=str(tmp_path / "sources")),
        services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
        schemas=SchemasSettings(schemas_dir=str(tmp_path / "schemas")),
        hunts=HuntsSettings(rules_dir=str(tmp_path / "rules"), hunt_dir=str(tmp_path / "hunts")),
        auth=AuthSettings(
            enabled=True,
            auth_dir=str(tmp_path / "auth"),
            local=LocalAuthSettings(enabled=True, admin_password=secrets.token_urlsafe(16)),
        ),
        secrets=SecretsSettings(provider="file", path=str(tmp_path / "secrets")),
        api=APISettings(jwt_secret="test-secret-key-for-unit-tests-hmac32"),
        gitops=GitopsSettings(
            enabled=True,
            local_path=str(tmp_path / "deploy"),
            push=False,
            pack=GitopsPackSettings(interval_seconds=3600.0),
        ),
    )


def test_the_lifespan_runs_the_consolidation_task_and_cancels_it(tmp_path: Path) -> None:
    app = create_app(settings=_settings(tmp_path))
    try:
        with TestClient(app, raise_server_exceptions=False):
            task = app.state.gitops_consolidation
            assert task.get_name() == "gitops-consolidation"
            assert not task.done()
        assert task.cancelled()
    finally:
        _registries.clear()
