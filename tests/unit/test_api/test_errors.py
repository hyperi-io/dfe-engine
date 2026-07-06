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
        # WWW-Authenticate must survive the shared HTTPException handler -
        # dropping it (JSONResponse built without headers=) is a live bug.
        assert resp.headers["www-authenticate"] == "Bearer"

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
        """Health endpoints should work without auth."""
        for path in ("/health/live", "/health/ready", "/health/startup"):
            resp = client.get(path)
            assert resp.status_code == 200, f"{path} returned {resp.status_code}"
            data = resp.json()
            assert "status" in data
