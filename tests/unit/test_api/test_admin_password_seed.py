#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_admin_password_seed.py
#  Purpose:      Regression - the configured local admin password reaches the seed
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The documented ``DFE_AUTH_LOCAL_OPERATOR_PASSWORD`` (settings.auth.local.operator_password)
must be applied to the bootstrapped admin. It was previously ignored (app startup
read a different env var), leaving the admin on the well-known default even when
an operator configured a password.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries
from dfe_engine.settings import (
    APISettings,
    AuthSettings,
    ClickHouseSettings,
    DFESettings,
    HuntsSettings,
    LocalAuthSettings,
    SchemasSettings,
    SecretsSettings,
    ServicesSettings,
    SourceSettings,
)

_CUSTOM_PW = "custom-admin-pw-that-is-plenty-long"


def _settings(tmp_path: Path) -> DFESettings:
    for sub in ("sources", "services", "rules", "hunts", "auth", "schemas", "secrets"):
        (tmp_path / sub).mkdir()
    return DFESettings(
        config_dir=str(tmp_path),
        clickhouse=ClickHouseSettings(bootstrap_tables=False),
        source=SourceSettings(sources_dir=str(tmp_path / "sources")),
        services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
        schemas=SchemasSettings(schemas_dir=str(tmp_path / "schemas")),
        hunts=HuntsSettings(rules_dir=str(tmp_path / "rules"), hunt_dir=str(tmp_path / "hunts")),
        auth=AuthSettings(
            enabled=True,
            auth_dir=str(tmp_path / "auth"),
            local=LocalAuthSettings(enabled=True, admin_password=_CUSTOM_PW),
        ),
        secrets=SecretsSettings(provider="file", path=str(tmp_path / "secrets")),
        api=APISettings(jwt_secret="test-secret-key-for-unit-tests-hmac32"),
    )


def test_admin_seeded_with_configured_password(tmp_path):
    app = create_app(settings=_settings(tmp_path))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            good = client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": _CUSTOM_PW},
            )
            assert good.status_code == 200, good.text
            assert "access_token" in good.json()

            # The old hardcoded default must NOT authenticate.
            bad = client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "changeme"},
            )
            assert bad.status_code == 401
    finally:
        _registries.clear()
