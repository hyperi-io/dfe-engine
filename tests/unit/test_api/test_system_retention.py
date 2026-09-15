#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_system_retention.py
#  Purpose:      GET /api/v1/system/retention reports the deployment default TTL
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The default TTL is the environment's alone; the console can read it, not set it."""

from __future__ import annotations


class TestGetRetention:
    def test_reports_the_environment_default(self, admin_headers, client):
        resp = client.get("/api/v1/system/retention", headers=admin_headers)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"default_ttl_days": 90}

    def test_viewer_cannot_read(self, client, viewer_headers):
        assert client.get("/api/v1/system/retention", headers=viewer_headers).status_code == 403

    def test_the_console_cannot_set_it(self, admin_headers, client):
        resp = client.put(
            "/api/v1/system/retention", headers=admin_headers, json={"default_ttl_days": 30}
        )

        assert resp.status_code == 405
