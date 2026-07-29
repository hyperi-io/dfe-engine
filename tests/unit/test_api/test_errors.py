"""Tests for error handling — error format, exception handlers."""

from fastapi.testclient import TestClient

from dfe_engine.api.errors import ErrorCode, ErrorResponse, FieldError


class TestErrorModels:
    """ErrorResponse and FieldError serialization."""

    def test_error_response_basic(self):
        err = ErrorResponse(code="not_found", message="Source not found")
        data = err.model_dump(mode="json")
        assert data["code"] == "not_found"
        assert data["message"] == "Source not found"
        assert data["errors"] == []

    def test_error_response_with_field_errors(self):
        err = ErrorResponse(
            code="validation_error",
            message="2 validation error(s)",
            errors=[
                FieldError(field="source", message="Required"),
                FieldError(field="schema_config.ttl_days", message="Must be positive"),
            ],
        )
        data = err.model_dump(mode="json")
        assert len(data["errors"]) == 2
        assert data["errors"][0]["field"] == "source"
        assert data["errors"][1]["field"] == "schema_config.ttl_days"

    def test_error_codes(self):
        assert ErrorCode.UNAUTHORIZED == "unauthorized"
        assert ErrorCode.FORBIDDEN == "forbidden"
        assert ErrorCode.NOT_FOUND == "not_found"
        assert ErrorCode.VALIDATION_ERROR == "validation_error"
        assert ErrorCode.INTERNAL_ERROR == "internal_error"


class TestExceptionHandlers:
    """Exception → HTTP response mapping."""

    def test_401_no_auth_header(self, client: TestClient):
        resp = client.get("/api/v1/auth/me")
        assert resp.status_code == 401
        data = resp.json()
        assert data["code"] == "unauthorized"
        assert "message" in data

    def test_401_bad_token(self, client: TestClient):
        resp = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer garbage"},
        )
        assert resp.status_code == 401

    def test_403_viewer_write(self, client: TestClient, viewer_headers: dict, sample_source: dict):
        resp = client.post("/api/v1/sources", json=sample_source, headers=viewer_headers)
        assert resp.status_code == 403
        data = resp.json()
        assert data["code"] == "forbidden"

    def test_404_source_not_found(self, client: TestClient, admin_headers: dict):
        resp = client.get("/api/v1/sources/nonexistent", headers=admin_headers)
        assert resp.status_code == 404
        data = resp.json()
        assert data["code"] == "not_found"

    def test_422_pydantic_validation(self, client: TestClient):
        # Missing required 'password' field in login
        resp = client.post("/api/v1/auth/login", json={"username": "admin"})
        assert resp.status_code == 422
        data = resp.json()
        assert data["code"] == "validation_error"
        assert len(data["errors"]) > 0
        assert data["errors"][0]["field"]  # field path present

    def test_error_response_shape_consistent(self, client: TestClient):
        """All error responses have code + message."""
        resp = client.get("/api/v1/sources/nonexistent")  # no auth
        data = resp.json()
        assert "code" in data
        assert "message" in data

    def test_health_no_auth(self, client: TestClient):
        """Health endpoints need no auth. Liveness is dependency-free (always
        200); readiness is fail-closed on ClickHouse (P2.5), so it is 200 when
        CH is reachable and 503 when not - both valid, neither an auth 401."""
        resp = client.get("/livez")
        assert resp.status_code == 200, f"/livez returned {resp.status_code}"
        assert "status" in resp.json()

        resp = client.get("/readyz")
        assert resp.status_code in (200, 503), f"/readyz returned {resp.status_code}"
        assert "status" in resp.json()

    def test_retired_health_paths_are_gone(self, client: TestClient):
        """The other half of the contract: retired spellings must 404.

        A probe path that quietly keeps answering is how a stale chart passes
        while pointing at a name the app no longer serves.
        """
        for path in ("/healthz", "/health/live", "/health/ready", "/health/startup", "/startupz"):
            resp = client.get(path)
            assert resp.status_code == 404, f"{path} still answers ({resp.status_code})"


class TestServiceUnavailable:
    """scalo ServiceUnavailable (CH resilience budget exhausted) -> 503, never 500."""

    @staticmethod
    def _app():
        from fastapi import FastAPI
        from scalo.resilience import ServiceUnavailable

        from dfe_engine.api.errors import install_exception_handlers

        app = FastAPI()
        install_exception_handlers(app)

        @app.get("/dead")
        def _dead():
            raise ServiceUnavailable("ClickHouse unreachable after 60s (3 attempts)")

        @app.get("/waking")
        def _waking():
            raise ServiceUnavailable("ClickHouse unreachable after 300s", waking=True)

        return app

    def test_budget_exhausted_maps_to_503(self):
        c = TestClient(self._app(), raise_server_exceptions=False)
        resp = c.get("/dead")
        assert resp.status_code == 503
        data = resp.json()
        assert data["code"] == "service_unavailable"
        assert data["context"]["waking"] is False
        assert "message" in data

    def test_waking_says_warming_up(self):
        c = TestClient(self._app(), raise_server_exceptions=False)
        resp = c.get("/waking")
        assert resp.status_code == 503
        data = resp.json()
        assert data["code"] == "service_unavailable"
        assert data["context"]["waking"] is True
        assert "warming up" in data["message"].lower()
