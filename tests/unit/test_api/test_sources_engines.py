#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_sources_engines.py
#  Purpose:      Listing the table engines a source may select
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.support.accounts import session_token


class TestListTableEngines:
    def test_lists_every_registry_engine_with_its_argument_rule(
        self, admin_headers: dict, client: TestClient
    ):
        resp = client.get("/api/v1/sources/engines", headers=admin_headers)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        engines = {item["name"]: item for item in body["items"]}
        assert body["total_size"] == len(engines)
        assert engines["MergeTree"]["arguments"] == "none"
        assert engines["MergeTree"]["argument_hint"] == ""
        assert engines["CollapsingMergeTree"]["arguments"] == "required"
        assert engines["ReplacingMergeTree"]["description"]

    def test_requires_authentication(self, client: TestClient):
        assert client.get("/api/v1/sources/engines").status_code == 401

    def test_requires_source_read(self, api_settings, app, client: TestClient):
        app.state.role_store.create(
            "no-sources", description="test", permissions=["lifecycle:read"]
        )
        app.state.role_config = app.state.role_store.load_config()
        app.state.group_store.create("grp-no-sources", roles=["no-sources"])
        app.state.account_store.create("nosrc", "pw-12345", groups=["grp-no-sources"])
        app.state.group_store.add_member("grp-no-sources", "nosrc")
        token = session_token(
            app,
            api_settings,
            {
                "groups": ["grp-no-sources"],
                "org_id": "test-org",
                "roles": ["no-sources"],
                "sub": "nosrc",
            },
        )

        resp = client.get("/api/v1/sources/engines", headers={"Authorization": f"Bearer {token}"})

        assert resp.status_code == 403
