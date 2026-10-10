#  Project:      dfe-engine
#  File:         tests/support/daemon_metrics_probe.py
#  Purpose:      The daemon's /metrics and MeterProviders, measured in a process of their own
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The engine daemon's metrics shape, built in a fresh interpreter and reported as JSON.

``tests/unit/test_api/test_daemon_metrics.py`` runs this as a child with a scratch
directory and reads the JSON it prints. OpenTelemetry's global MeterProvider can be
set once per process, so the daemon's shape -- ServiceApp's manager installed first,
the API built on it -- needs a process no other test has touched. The name keeps it
out of normal collection.

The app serves one request and runs its lifespan twice, so the schema phase runs two
passes through the wiring the daemon uses.
"""

import gc
import json
import logging
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from scalo.metrics import create_metrics

from dfe_engine.api.app import create_app
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

OVERRIDE_REFUSED = "Overriding of current MeterProvider is not allowed"
SCHEMA_PASSES = 2


class _Recorder(logging.Handler):
    """Every message OpenTelemetry logs at WARNING or above."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _settings(root: Path) -> DFESettings:
    """Hermetic settings under *root*: YAML stores, no ClickHouse table bootstrap."""
    dirs = {}
    for name in ("sources", "services", "rules", "hunts", "auth", "schemas", "secrets"):
        dirs[name] = root / name
        dirs[name].mkdir(parents=True, exist_ok=True)
    return DFESettings(
        config_dir=str(root),
        clickhouse=ClickHouseSettings(bootstrap_tables=False),
        source=SourceSettings(sources_dir=str(dirs["sources"])),
        services=ServicesSettings(config_yaml_dir=str(dirs["services"])),
        schemas=SchemasSettings(schemas_dir=str(dirs["schemas"])),
        hunts=HuntsSettings(rules_dir=str(dirs["rules"]), hunt_dir=str(dirs["hunts"])),
        auth=AuthSettings(
            enabled=True,
            auth_dir=str(dirs["auth"]),
            local=LocalAuthSettings(admin_password="probe-admin-pw"),
        ),
        secrets=SecretsSettings(provider="file", path=str(dirs["secrets"])),
        api=APISettings(jwt_secret="probe-secret-key-for-the-metrics-probe", jwt_expire_minutes=5),
    )


def _meter_providers() -> int:
    """How many SDK MeterProviders this process holds."""
    return sum(1 for obj in gc.get_objects() if isinstance(obj, MeterProvider))


def main(root: Path) -> int:
    """Build the daemon's metrics shape under *root* and print what it exposes."""
    recorder = _Recorder()
    logging.getLogger("opentelemetry").addHandler(recorder)

    # ServiceApp's manager: OpenTelemetry backend, Prometheus scrape on, push off here.
    manager = create_metrics(
        "dfe_engine", backend="opentelemetry", backend_config={"opentelemetry": {"enabled": False}}
    )
    app = create_app(settings=_settings(root), metrics_manager=manager)
    for _ in range(SCHEMA_PASSES):
        with TestClient(app, raise_server_exceptions=False) as client:
            client.get("/api/v1/apps/dfe-loader/default/status")

    report = {
        "exposition": manager.metrics.decode(),
        "meter_providers": _meter_providers(),
        "override_refusals": sum(OVERRIDE_REFUSED in m for m in recorder.messages),
    }
    sys.stdout.write(json.dumps(report) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
