#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_docs_exposure.py
#  Purpose:      Swagger UI and ReDoc are served in a dev posture only, unless configured
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""/docs and /redoc load their scripts from a public CDN into the engine's origin.

The gateway serves the engine on the console's own hostname, so a script those
pages pull in runs beside the console's session. Outside a dev posture they are
off unless ``api.docs_enabled`` turns them on; ``/openapi.json`` stays, because
deployment tooling reads it.
"""

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app, docs_served
from dfe_engine.settings import DFESettings


def _with(settings: DFESettings, *, env: str, docs_enabled: bool | None) -> DFESettings:
    api = settings.api.model_copy(update={"docs_enabled": docs_enabled})
    return settings.model_copy(update={"env": env, "api": api})


def _statuses(settings: DFESettings) -> dict[str, int]:
    client = TestClient(create_app(settings=settings))
    return {path: client.get(path).status_code for path in ("/docs", "/redoc", "/openapi.json")}


@pytest.mark.parametrize(
    ("env", "docs_enabled", "served"),
    [
        ("production", None, False),
        ("staging", None, False),
        ("test", None, True),
        ("dev", None, True),
        ("production", True, True),
        ("dev", False, False),
    ],
)
def test_the_docs_pages_follow_the_posture_unless_configured(
    api_settings, env, docs_enabled, served
):
    settings = _with(api_settings, env=env, docs_enabled=docs_enabled)

    statuses = _statuses(settings)

    assert docs_served(settings) is served
    expected = 200 if served else 404
    assert statuses["/docs"] == expected
    assert statuses["/redoc"] == expected
    assert statuses["/openapi.json"] == 200
