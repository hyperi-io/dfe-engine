"""A request body over its limit is answered 413 before anything past the limit is read."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from dfe_engine.api.body_limits import RequestBodyCapMiddleware, own_limit
from dfe_engine.settings import DFESettings

LIMIT = 4096
JSON = {"content-type": "application/json"}
SERVICE = "/api/v1/services/customsvc/capped"


def _json_of(size: int) -> bytes:
    """A one-key JSON object of exactly ``size`` bytes."""
    shell = b'{"note": ""}'
    return b'{"note": "' + b"a" * (size - len(shell)) + b'"}'


def _refusal(limit: int, setting: str, env: str) -> dict[str, object]:
    """The 413 body, in the same envelope every other API error uses."""
    return {
        "code": "payload_too_large",
        "message": (
            f"The request body is larger than the {limit}-byte limit. "
            f"Raise {setting} ({env}) to accept a larger one."
        ),
        "errors": [],
    }


API_REFUSAL = _refusal(LIMIT, "api.max_request_body_bytes", "DFE_API_MAX_REQUEST_BODY_BYTES")


class TestTheServiceConfigWrite:
    """The engine's own app, on the service-config write."""

    @pytest.fixture(autouse=True)
    def _capped(self, app):
        app.state.settings.api.max_request_body_bytes = LIMIT

    def test_a_config_at_the_cap_is_saved(self, client, admin_headers):
        resp = client.put(SERVICE, content=_json_of(LIMIT), headers={**admin_headers, **JSON})

        assert resp.status_code == 200, resp.text
        saved = client.get(SERVICE, headers=admin_headers).json()["config"]
        assert len(saved["note"]) == LIMIT - len(b'{"note": ""}')

    def test_a_config_one_byte_over_is_413_and_not_saved(self, client, admin_headers):
        resp = client.put(SERVICE, content=_json_of(LIMIT + 1), headers={**admin_headers, **JSON})

        assert resp.status_code == 413
        assert resp.json() == API_REFUSAL
        assert client.get(SERVICE, headers=admin_headers).status_code == 404

    def test_the_setting_moves_the_limit(self, app, client, admin_headers):
        body = _json_of(2 * LIMIT)
        headers = {**admin_headers, **JSON}
        assert client.put(SERVICE, content=body, headers=headers).status_code == 413

        app.state.settings.api.max_request_body_bytes = 2 * LIMIT

        assert client.put(SERVICE, content=body, headers=headers).status_code == 200

    def test_an_oversized_body_is_refused_before_the_caller_is_authenticated(self, client):
        assert client.put(SERVICE, content=_json_of(LIMIT), headers=JSON).status_code == 401

        resp = client.put(SERVICE, content=_json_of(LIMIT + 1), headers=JSON)

        assert resp.status_code == 413
        assert resp.json() == API_REFUSAL

    def test_a_read_is_not_capped(self, client, admin_headers):
        resp = client.get("/api/v1/services", headers=admin_headers)

        assert resp.status_code == 200


class TestTheEngineUploadRoutes:
    """Each upload route, mounted through its included router, keeps its own limit."""

    OVER_THE_API_CAP = 2 * LIMIT

    @pytest.fixture(autouse=True)
    def _capped(self, app):
        app.state.settings.api.max_request_body_bytes = LIMIT

    def test_an_app_file_write(self, client, admin_headers):
        body = {"content": "a" * self.OVER_THE_API_CAP}

        resp = client.put(
            "/api/v1/apps/dfe-transform-vrl/edge/files/transforms/000_parse.vrl",
            json=body,
            headers=admin_headers,
        )

        assert resp.status_code != 413, resp.text

    def test_a_repository_object(self, client, admin_headers):
        resp = client.put(
            "/api/v1/repository/objects/user/admin/notes/draft",
            content=b"a" * self.OVER_THE_API_CAP,
            headers=admin_headers,
        )

        assert resp.status_code != 413, resp.text

    def test_an_elastic_template_upload(self, client, admin_headers):
        template = b'{"mappings": {"properties": {}}, "pad": "' + b"a" * self.OVER_THE_API_CAP
        files = {"file": ("template.json", template + b'"}', "application/json")}

        resp = client.post("/api/v1/schemas/elastic-converter", files=files, headers=admin_headers)

        assert resp.status_code != 413, resp.text

    def test_a_config_write_of_the_same_size_is_refused(self, client, admin_headers):
        resp = client.put(
            SERVICE, content=_json_of(self.OVER_THE_API_CAP), headers={**admin_headers, **JSON}
        )

        assert resp.status_code == 413
        assert resp.json() == API_REFUSAL


@pytest.fixture
def probe(api_settings: DFESettings) -> Iterator[TestClient]:
    """A bare app behind the cap: one config route, one upload route with its own bound."""
    application = FastAPI()
    application.state.settings = api_settings
    api_settings.api.max_request_body_bytes = LIMIT
    api_settings.repository.max_object_bytes = 4 * LIMIT
    application.add_middleware(RequestBodyCapMiddleware)

    @application.put("/config")
    async def put_config(body: dict[str, Any]) -> dict[str, int]:
        return {"note_bytes": len(body["note"])}

    @application.put("/upload")
    @own_limit("repository.max_object_bytes", lambda s: s.repository.max_object_bytes)
    async def put_upload(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    with TestClient(application, raise_server_exceptions=False) as client:
        yield client


class TestTheCap:
    def test_the_route_reads_the_body_it_was_sent(self, probe):
        body = _json_of(LIMIT)

        resp = probe.put("/config", content=body, headers=JSON)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"note_bytes": LIMIT - len(b'{"note": ""}')}

    def test_a_chunked_body_with_no_length_is_counted_as_it_arrives(self, probe):
        chunks = [b'{"note": "', b"a" * LIMIT, b'"}']

        resp = probe.put("/config", content=iter(chunks), headers=JSON)

        assert resp.status_code == 413
        assert resp.json() == API_REFUSAL

    def test_a_declared_length_over_the_cap_is_refused_unread(self, probe):
        resp = probe.put(
            "/config",
            content=iter([b'{"note": "x"}']),
            headers={**JSON, "content-length": str(LIMIT + 1)},
        )

        assert resp.status_code == 413
        assert resp.json() == API_REFUSAL

    def test_an_upload_is_held_to_its_own_limit_not_the_api_cap(self, probe):
        resp = probe.put("/upload", content=b"x" * (4 * LIMIT))

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"bytes": 4 * LIMIT}

    def test_an_upload_over_its_own_limit_names_its_own_setting(self, probe):
        resp = probe.put("/upload", content=b"x" * (4 * LIMIT + 1))

        assert resp.status_code == 413
        assert resp.json() == _refusal(
            4 * LIMIT, "repository.max_object_bytes", "DFE_REPOSITORY_MAX_OBJECT_BYTES"
        )

    def test_an_empty_body_passes(self, probe):
        resp = probe.put("/upload")

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"bytes": 0}
