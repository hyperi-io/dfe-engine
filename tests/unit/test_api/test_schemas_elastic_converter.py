#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_schemas_elastic_converter.py
#  Purpose:      POST /api/v1/schemas/elastic-converter
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, UploadFile
from starlette.requests import Request

from dfe_engine.api.v1.schemas import elastic_converter
from dfe_engine.auth.models import AuthContext
from dfe_engine.settings import DFESettings, get_settings, load_settings, reset_settings
from tests.unit.test_services.test_elastic_schema_service import MINIMAL_BEAT_TEMPLATE

_LIMIT_ENV = "DFE_API_ELASTIC_CONVERTER_MAX_UPLOAD_BYTES"


@pytest.fixture(autouse=True)
def _reset_settings_after_each_test() -> Iterator[None]:
    yield
    reset_settings()


def _detail(refused: pytest.ExceptionInfo[HTTPException]) -> dict[str, Any]:
    """The error body a refusal carries."""
    detail = refused.value.detail
    assert isinstance(detail, dict), detail
    return detail


def _http_request(
    settings: DFESettings, extra_headers: list[tuple[bytes, bytes]] | None = None
) -> Request:
    """A converter request served by an app whose settings are ``settings``."""
    app = FastAPI()
    app.state.settings = settings
    headers = list(extra_headers or [])
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/schemas/elastic-converter",
            "raw_path": b"/api/v1/schemas/elastic-converter",
            "root_path": "",
            "query_string": b"",
            "headers": headers,
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "app": app,
        }
    )


class _ExplodingReader(io.BytesIO):
    def read(self, size: int | None = -1) -> bytes:
        raise OSError("simulated upload read failure")


async def test_elastic_converter_upload_read_error_returns_422() -> None:
    uf = UploadFile(file=_ExplodingReader(), filename="x.json")
    user = AuthContext(user_id="tester")
    with pytest.raises(HTTPException) as ri:
        await elastic_converter(request=_http_request(load_settings()), user=user, file=uf)
    assert ri.value.status_code == 422
    assert _detail(ri)["code"] == "upload_read_error"


async def test_elastic_converter_upload_exceeds_max_returns_413(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_LIMIT_ENV, "48")
    uf = UploadFile(filename="big.json", file=io.BytesIO(b"y" * 49))
    user = AuthContext(user_id="tester")
    with pytest.raises(HTTPException) as ri:
        await elastic_converter(request=_http_request(load_settings()), user=user, file=uf)
    assert ri.value.status_code == 413
    assert _detail(ri)["code"] == "upload_too_large"


async def test_elastic_converter_declared_content_length_too_large_returns_413(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_LIMIT_ENV, "500")
    uf = UploadFile(filename="x.json", file=io.BytesIO(b"{}"))
    user = AuthContext(user_id="tester")
    req = _http_request(load_settings(), [(b"content-length", b"99999999")])
    with pytest.raises(HTTPException) as ri:
        await elastic_converter(request=req, user=user, file=uf)
    assert ri.value.status_code == 413
    assert _detail(ri)["code"] == "upload_too_large"


async def test_a_config_file_limit_reaches_the_converter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The app's settings can carry a config file's values; get_settings() reads no file.
    monkeypatch.delenv(_LIMIT_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "dfe.yaml"
    config.write_text("api:\n  elastic_converter_max_upload_bytes: 256\n", encoding="utf-8")
    from_file = load_settings(str(config))
    assert get_settings().api.elastic_converter_max_upload_bytes > 257

    uf = UploadFile(filename="big.json", file=io.BytesIO(b"y" * 257))
    user = AuthContext(user_id="tester")
    with pytest.raises(HTTPException) as ri:
        await elastic_converter(request=_http_request(from_file), user=user, file=uf)
    assert ri.value.status_code == 413
    assert _detail(ri)["code"] == "upload_too_large"
    assert "(256 bytes)" in _detail(ri)["message"]


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


def test_elastic_converter_large_upload_returns_413(app, client, admin_headers) -> None:
    app.state.settings.api.elastic_converter_max_upload_bytes = 256
    blob = b"z" * 257
    resp = client.post(
        "/api/v1/schemas/elastic-converter",
        files={"file": ("big.json", blob, "application/json")},
        headers=admin_headers,
    )
    assert resp.status_code == 413
    assert resp.json()["code"] == "upload_too_large"


def test_an_upload_at_the_app_limit_is_converted(app, client, admin_headers) -> None:
    template = json.dumps(MINIMAL_BEAT_TEMPLATE).encode("utf-8")
    app.state.settings.api.elastic_converter_max_upload_bytes = len(template)
    resp = client.post(
        "/api/v1/schemas/elastic-converter",
        files={"file": ("t.json", template, "application/json")},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
