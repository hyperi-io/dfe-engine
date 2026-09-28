#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_system_status.py
#  Purpose:      GET /system/status reports a refused YAML write until its file writes again
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A write refused by the YAML writer shows on the status report and on the engine's metrics.

The trigger is real: a NEL character reads back from YAML as a line break, so a group
description holding one is refused rather than stored changed.
"""

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from scalo.metrics import create_metrics

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries
from dfe_engine.yaml_health import WRITE_FAILURES, YamlWriteMetrics, write_health

STATUS = "/api/v1/system/status"
GROUPS = "/api/v1/auth/groups"


@pytest.fixture(autouse=True)
def _clear_write_health():
    yield
    write_health().bind(YamlWriteMetrics())
    for entry in write_health().degraded():
        write_health().written(entry.target)


def _failures(manager, reason: str) -> float:
    for family in text_string_to_metric_families(manager.metrics_text):
        for sample in family.samples:
            if sample.name == WRITE_FAILURES and sample.labels.get("reason") == reason:
                return sample.value
    return 0.0


def test_a_refused_write_is_degraded_until_its_file_writes_again(
    client: TestClient, admin_headers: dict
):
    created = client.post(
        GROUPS, json={"name": "nel", "roles": [], "description": "before"}, headers=admin_headers
    )
    assert created.status_code == 201, created.text
    healthy = client.get(STATUS, headers=admin_headers)

    refused = client.put(f"{GROUPS}/nel", json={"description": "a\x85b"}, headers=admin_headers)
    degraded = client.get(STATUS, headers=admin_headers)
    kept = client.get(f"{GROUPS}/nel", headers=admin_headers)
    fixed = client.put(f"{GROUPS}/nel", json={"description": "after"}, headers=admin_headers)
    cleared = client.get(STATUS, headers=admin_headers)

    assert healthy.json() == {"status": "ok", "degraded": []}
    assert refused.status_code == 500, refused.text
    assert degraded.status_code == 200, degraded.text
    assert degraded.json()["status"] == "degraded"
    [condition] = degraded.json()["degraded"]
    assert condition["kind"] == "yaml_write"
    assert condition["target"].endswith("nel.yaml")
    assert condition["reason"] == "verify"
    assert kept.json()["description"] == "before"
    assert fixed.status_code == 200, fixed.text
    assert cleared.json() == {"status": "ok", "degraded": []}


def test_the_status_report_needs_system_read(client: TestClient, viewer_headers: dict):
    assert client.get(STATUS).status_code == 401
    assert client.get(STATUS, headers=viewer_headers).status_code == 403


def test_a_refused_write_is_counted_on_the_engines_metrics(api_settings, admin_headers):
    manager = create_metrics("test", backend="prometheus", enable_auto_update=False)
    app = create_app(settings=api_settings, metrics_manager=manager)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            # The seed issues admin a password it must change; setting it clears that.
            app.state.account_store.reset_password("admin", api_settings.auth.local.admin_password)
            client.post(GROUPS, json={"name": "nel", "roles": []}, headers=admin_headers)
            refused = client.put(
                f"{GROUPS}/nel", json={"description": "a\x85b"}, headers=admin_headers
            )
    finally:
        _registries.clear()

    assert refused.status_code == 500, refused.text
    assert _failures(manager, "verify") == 1.0
