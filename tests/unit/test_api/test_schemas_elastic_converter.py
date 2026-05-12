#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_schemas_elastic_converter.py
#  Purpose:      POST /api/v1/schemas/elastic-converter
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from dfe_engine.api.v1.schemas import elastic_converter
from dfe_engine.auth.models import AuthContext
from tests.unit.test_services.test_elastic_schema_service import MINIMAL_BEAT_TEMPLATE


class _ExplodingReader:
    def read(self, size: int = -1) -> bytes:
        raise OSError("simulated upload read failure")


async def test_elastic_converter_upload_read_error_returns_422() -> None:
    uf = UploadFile(file=_ExplodingReader(), filename="x.json")
    user = AuthContext(user_id="tester")
    with pytest.raises(HTTPException) as ri:
        await elastic_converter(user=user, file=uf)
    assert ri.value.status_code == 422
    assert ri.value.detail["code"] == "upload_read_error"


def test_elastic_converter_upload_json(client, admin_headers) -> None:
    payload = json.dumps(MINIMAL_BEAT_TEMPLATE)
    resp = client.post(
        "/api/v1/schemas/elastic-converter",
        files={"file": ("t.json", payload.encode("utf-8"), "application/json")},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    names = {row["name"] for row in body}
    assert "container_name" in names
    assert "timestamp" in names


def test_elastic_converter_invalid_template_returns_422(client, admin_headers) -> None:
    resp = client.post(
        "/api/v1/schemas/elastic-converter",
        files={"file": ("bad.json", b"{", "application/json")},
        headers=admin_headers,
    )
    assert resp.status_code == 422
    data = resp.json()
    assert data["code"] == "elastic_convert_error"
    assert "Invalid JSON" in data["message"]
