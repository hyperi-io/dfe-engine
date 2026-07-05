#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_app_docs_posture.py
#  Purpose:      Docs/OpenAPI surface gated on deployment posture (F-DOCS-EXPOSED)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Swagger/ReDoc/OpenAPI are served only in a dev posture.

/docs, /redoc and /openapi.json enumerate every route (incl. gitops/governance/
admin), so a non-dev posture returns 404 for all three; a dev posture serves them.
The requests below never enter the TestClient context manager, so the app
lifespan (registry/auth bootstrap) does not run - only routing is exercised.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.settings import APISettings, AuthSettings, DFESettings


def _client(env: str) -> TestClient:
    settings = DFESettings(
        env=env,
        auth=AuthSettings(enabled=False),
        api=APISettings(jwt_secret="unit-test-jwt-secret-key-0123456789"),
    )
    return TestClient(create_app(settings=settings))


class TestDocsPosture:
    def test_dev_posture_serves_docs(self):
        client = _client("dev")
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/docs").status_code == 200
        assert client.get("/redoc").status_code == 200

    def test_non_dev_posture_hides_docs(self):
        client = _client("production")
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
