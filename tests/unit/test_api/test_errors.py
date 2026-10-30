"""Tests for error handling -- error format, exception handlers."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dfe_engine.api.errors import ErrorCode, ErrorResponse, FieldError
from dfe_engine.gitcrud.commit_policy import NAME_MAX
from dfe_engine.settings import DFESettings, GitopsSettings


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
    """Exception -> HTTP response mapping."""

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
        200), and readiness is fail-closed on ClickHouse, so it is 200 when CH is
        reachable and 503 when not - both valid, neither an auth 401."""
        resp = client.get("/livez")
        assert resp.status_code == 200, f"/livez returned {resp.status_code}"
        assert "status" in resp.json()

        resp = client.get("/readyz")
        assert resp.status_code in (200, 503), f"/readyz returned {resp.status_code}"
        assert "status" in resp.json()

    def test_retired_health_aliases_are_gone(self, client: TestClient):
        """The old spellings must 404, not answer.

        scalo 2.29.12 retired them. An alias kept alive still answers 200, so a
        chart left probing the old name keeps passing and the migration looks
        finished when it is not - which is how this service's chart probed
        /health/live against a scalo that had stopped serving it. Asserting the
        404 is what makes a re-added alias fail a test instead of hiding.
        """
        for path in ("/health/live", "/health/ready", "/health/startup", "/healthz", "/startupz"):
            resp = client.get(path)
            assert resp.status_code == 404, f"{path} still answers ({resp.status_code})"


class TestTheRouterAnswersInTheEnvelope:
    """A path no route matches, or a method its route does not take, answers ErrorResponse."""

    def test_an_unknown_path(self, client: TestClient):
        resp = client.get("/api/v1/no-such-route")

        assert resp.status_code == 404
        assert resp.json() == {"code": "request_error", "message": "Not Found", "errors": []}

    def test_a_method_the_route_does_not_take(self, client: TestClient, admin_headers: dict):
        resp = client.patch("/api/v1/sources", headers=admin_headers)

        assert resp.status_code == 405
        assert resp.json() == {
            "code": "request_error",
            "message": "Method Not Allowed",
            "errors": [],
        }
        # The router's Allow header survives the envelope.
        assert resp.headers["allow"]


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


class TestAnUnexpectedFailure:
    """The catch-all 500 answers in the envelope the caller's client parses."""

    @staticmethod
    def _client() -> TestClient:
        from fastapi import FastAPI

        from dfe_engine.api.errors import install_exception_handlers
        from dfe_engine.api.v1.scim import SCIM_ROOT

        app = FastAPI()
        install_exception_handlers(app)

        @app.get(f"{SCIM_ROOT}/Users/broken")
        def _scim_broken():
            raise RuntimeError("volume went away")

        @app.get("/api/v1/broken")
        def _native_broken():
            raise RuntimeError("volume went away")

        return TestClient(app, raise_server_exceptions=False)

    def test_a_scim_route_answers_a_scim_error(self):
        resp = self._client().get("/api/v1/scim/v2/Users/broken")

        assert resp.status_code == 500
        assert resp.headers["content-type"].startswith("application/scim+json")
        assert resp.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
        assert resp.json()["detail"] == "An unexpected error occurred"
        assert "volume went away" not in resp.text

    def test_a_native_route_keeps_the_native_envelope(self):
        resp = self._client().get("/api/v1/broken")

        assert resp.status_code == 500
        assert resp.headers["content-type"].startswith("application/json")
        assert resp.json() == {
            "code": ErrorCode.INTERNAL_ERROR,
            "message": "An unexpected error occurred",
            "errors": [],
        }


class TestScaloApiHelpersAnswerInTheEnvelope:
    """A refusal from scalo.api's write and list helpers reaches the caller as ErrorResponse."""

    @staticmethod
    def _client() -> TestClient:
        from fastapi import FastAPI
        from scalo.api import ListParams, PreconditionHeaders, ProblemError, paginate

        from dfe_engine.api.errors import install_exception_handlers

        app = FastAPI()
        install_exception_handlers(app)

        @app.put("/things/{name}")
        def _apply(name: str, pre: PreconditionHeaders) -> dict:
            pre.check('"current"')
            return {"name": name}

        @app.get("/things")
        def _list(query: ListParams) -> dict:
            return paginate(["a", "b", "c"], query).model_dump()

        @app.get("/fields")
        def _fields() -> None:
            from scalo.api import FieldError as ProblemField

            raise ProblemError(
                422,
                "bad",
                code="invalid",
                errors=[ProblemField(field="spec.size", detail="too big")],
            )

        return TestClient(app, raise_server_exceptions=False)

    def test_a_stale_etag_is_412_with_the_current_one(self):
        resp = self._client().put("/things/x", headers={"If-Match": '"stale"'})

        assert resp.status_code == 412
        assert resp.json() == {
            "code": "etag_mismatch",
            "message": "If-Match does not match the current ETag",
            "errors": [],
            "context": {"etag": '"current"'},
        }

    def test_a_write_with_no_precondition_is_428(self):
        resp = self._client().put("/things/x")

        assert resp.status_code == 428
        assert resp.json()["code"] == "precondition_required"

    def test_a_page_token_from_elsewhere_is_400(self):
        resp = self._client().get("/things", params={"page_token": "not-ours"})

        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_page_token"
        assert "context" not in resp.json()

    def test_field_errors_keep_their_field(self):
        resp = self._client().get("/fields")

        assert resp.status_code == 422
        assert resp.json()["errors"] == [
            {"field": "spec.size", "message": "too big", "code": ErrorCode.VALIDATION_ERROR}
        ]


@pytest.fixture
def on_a_deploy_repo(api_settings: DFESettings) -> DFESettings:
    """The API fixtures' settings on a local deploy repo. Request it ahead of ``client``."""
    api_settings.gitops = GitopsSettings(
        enabled=True, local_path=str(Path(api_settings.config_dir) / "deploy"), push=False
    )
    return api_settings


_RULE = {"name": "r1", "user_sql": "SELECT * FROM dfe.main WHERE 1 = 1"}


class TestABadRequestNeverAnswers500:
    """Malformed bodies and names a store cannot hold answer 4xx in the envelope, never 500."""

    @pytest.mark.parametrize(
        ("authed", "method", "path", "body", "status", "code"),
        [
            # The core-resource guard reads the body before auth runs.
            pytest.param(
                False,
                "POST",
                "/api/v1/field-maps",
                b"[]",
                401,
                "unauthorized",
                id="guard-json-array",
            ),
            pytest.param(
                False,
                "POST",
                "/api/v1/field-maps",
                b"null",
                401,
                "unauthorized",
                id="guard-json-null",
            ),
            pytest.param(
                False,
                "POST",
                "/api/v1/field-maps",
                b'"x"',
                401,
                "unauthorized",
                id="guard-json-string",
            ),
            pytest.param(
                False,
                "DELETE",
                "/api/v1/schemas/definitions/%2e%2e",
                b"",
                401,
                "unauthorized",
                id="guard-dotdot-schema",
            ),
            pytest.param(
                True,
                "GET",
                "/api/v1/schemas/definitions/%2e%2e/export",
                b"",
                422,
                "validation_error",
                id="schema-dotdot",
            ),
            pytest.param(
                True, "GET", "/api/v1/sources/a%20b", b"", 400, "invalid_name", id="source-space"
            ),
            pytest.param(
                True, "GET", "/api/v1/sources/x%00y", b"", 400, "invalid_name", id="source-nul"
            ),
            pytest.param(
                True,
                "GET",
                "/api/v1/sources/" + "a" * (NAME_MAX + 1),
                b"",
                400,
                "invalid_name",
                id="source-too-long",
            ),
            pytest.param(
                True,
                "PUT",
                "/api/v1/helm/files/" + "a" * 300 + "/vars/x",
                b'{"value": 1}',
                400,
                "invalid_name",
                id="helm-file-too-long",
            ),
            pytest.param(
                True,
                "GET",
                "/api/v1/helm/files/no-such-file/vars",
                b"",
                404,
                "not_found",
                id="helm-file-missing",
            ),
            pytest.param(
                True,
                "POST",
                "/api/v1/rules",
                {**_RULE, "severity": "x"},
                422,
                "validation_error",
                id="rule-severity",
            ),
        ],
    )
    def test_refused_in_the_envelope(
        self,
        on_a_deploy_repo: DFESettings,
        client: TestClient,
        admin_headers: dict,
        authed: bool,
        method: str,
        path: str,
        body: bytes | dict,
        status: int,
        code: str,
    ):
        headers = {"Content-Type": "application/json", **(admin_headers if authed else {})}
        if isinstance(body, dict):
            resp = client.request(method, path, json=body, headers=headers)
        else:
            resp = client.request(method, path, content=body, headers=headers)

        assert (resp.status_code, resp.json()["code"]) == (status, code), resp.text
