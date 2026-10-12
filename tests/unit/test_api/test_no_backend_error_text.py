#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_no_backend_error_text.py
#  Purpose:      A backend's own error text reaches the engine log, never a response
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Every route that reports a ClickHouse, Kafka, git or HTTP failure keeps the backend's text out.

ClickHouse's error text can carry statement fragments, user names and password
hashes, and these routes reach viewers through the console. Each test makes the
backend fail with :data:`SENTINEL`, then checks the response carries none of it
and the engine log carries all of it.
"""

import json
from types import SimpleNamespace

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from scalo.resilience import ServiceUnavailable

from dfe_engine.api.deps import get_clickhouse_client
from dfe_engine.api.errors import (
    GITOPS_UNAVAILABLE_MESSAGE,
    SERVICE_UNAVAILABLE_MESSAGE,
    engine_message,
    install_exception_handlers,
)
from dfe_engine.api.v1 import orgs as orgs_module
from dfe_engine.api.v1 import sources as sources_module
from dfe_engine.api.v1 import system as system_module
from dfe_engine.api.v1.apps import metrics_unavailable
from dfe_engine.api.v1.schemas import _promotion_refused
from dfe_engine.appmgmt import MetricsUnavailableError
from dfe_engine.auth.models import AuthContext
from dfe_engine.clickhouse.clickhouse_manager import ClickHouseManager
from dfe_engine.clickhouse.cloud import CloudServiceError
from dfe_engine.gitcrud import GitCrud, default_registry
from dfe_engine.gitops.repo import GitopsRepo, GitopsUnavailableError
from dfe_engine.kafka.topics import (
    BROKER_REFUSED,
    BROKER_UNREACHABLE,
    TopicSpec,
    ensure_topics,
    topic_status,
)
from dfe_engine.orgs.available_ids import OrgIdDiscoveryError
from dfe_engine.query.executor import ViewExecutionError
from dfe_engine.sampling import SamplerError
from dfe_engine.sampling.models import SampleBackend, SampleRequest
from dfe_engine.sampling.service import Sampler
from dfe_engine.schema import phase
from dfe_engine.schema.applier import SchemaApplyError
from dfe_engine.schema.manifest_applier import ManifestApplyError
from dfe_engine.schema.retention import CLICKHOUSE_REFUSED, apply_pinned_defaults
from dfe_engine.services.schema.json_promotion_service import JsonPromotionError
from dfe_engine.source.registry import SourceNotFoundError

FAKE_HASH = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
SENTINEL = f"Code: 516. DB::Exception: ALTER USER dfe_org_acme IDENTIFIED WITH sha256_hash BY '{FAKE_HASH}'"


def assert_hidden(text: str) -> None:
    """The response text carries no part of the backend's error."""
    assert "IDENTIFIED" not in text
    assert FAKE_HASH not in text


def assert_logged(events: list[dict]) -> None:
    """Some log line carries the backend's error whole, in its message or its error field."""
    assert any(FAKE_HASH in f"{e.get('event', '')} {e.get('error', '')}" for e in events), events


def _refuse(*_args, **_kwargs):
    raise RuntimeError(SENTINEL)


def _chained(kind: type[Exception], message: str) -> Exception:
    """``kind(message)`` raised from a backend error carrying the sentinel, as the engine raises it."""
    try:
        raise RuntimeError(SENTINEL)
    except RuntimeError as backend:
        try:
            raise kind(f"{message}: {backend}") from backend
        except kind as wrapped:
            return wrapped


class TestCentralHandlers:
    @staticmethod
    def _client() -> TestClient:
        app = FastAPI()
        install_exception_handlers(app)

        @app.get("/dead")
        def _dead():
            raise ServiceUnavailable(f"ClickHouse unreachable after 60s (3 attempts): {SENTINEL}")

        @app.get("/gitops")
        def _gitops():
            raise GitopsUnavailableError(f"deploy repo unreachable after 30s: {SENTINEL}")

        return TestClient(app, raise_server_exceptions=False)

    def test_an_exhausted_backend_answers_503_without_its_text(self, audit_events):
        resp = self._client().get("/dead")

        assert resp.status_code == 503
        assert resp.json()["message"] == SERVICE_UNAVAILABLE_MESSAGE
        assert_hidden(resp.text)
        assert_logged(audit_events)

    def test_an_unreachable_deploy_repo_answers_503_without_its_text(self, audit_events):
        resp = self._client().get("/gitops")

        assert resp.status_code == 503
        assert resp.json()["message"] == GITOPS_UNAVAILABLE_MESSAGE
        assert resp.headers["Retry-After"]
        assert_hidden(resp.text)
        assert_logged(audit_events)


class TestEngineMessage:
    def test_a_message_the_engine_composed_is_kept(self):
        refusal = CloudServiceError("CH Cloud service 'dev' not found. Available: prod (running)")

        assert engine_message(refusal, "fallback", event="e") == str(refusal)

    def test_a_message_raised_from_a_backend_is_replaced_and_logged(self, audit_events):
        failure = _chained(CloudServiceError, "GET /organizations failed")

        assert engine_message(failure, "fallback", event="e") == "fallback"
        assert_logged(audit_events)


class TestQueries:
    def test_a_failed_view_names_the_view_not_clickhouses_text(
        self, app, client, viewer_headers, audit_events
    ):
        class _FailingExecutor:
            def execute(self, **_kwargs):
                raise ViewExecutionError(f"Failed to execute view 'x': {SENTINEL}")

        app.state.view_executor = _FailingExecutor()

        resp = client.post(
            "/api/v1/queries/views/analytics/events/execute", headers=viewer_headers, json={}
        )

        assert resp.status_code == 500, resp.text
        assert resp.json()["code"] == "query_error"
        assert_hidden(resp.text)
        assert_logged(audit_events)

    def test_the_cost_leaderboard_hides_why_it_is_unavailable(
        self, client, viewer_headers, monkeypatch, audit_events
    ):
        monkeypatch.setattr(ClickHouseManager, "get_instance", _refuse)

        resp = client.get("/api/v1/queries/cost-leaderboard", headers=viewer_headers)

        assert resp.status_code == 503, resp.text
        assert_hidden(resp.text)
        assert_logged(audit_events)


class TestDiscovery:
    def test_a_connection_failure_is_503_without_clickhouses_text(
        self, app, client, admin_headers, audit_events
    ):
        app.state.connection_registry = SimpleNamespace(get_client=_refuse)

        resp = client.get("/api/v1/discovery/databases", headers=admin_headers)

        assert resp.status_code == 503, resp.text
        assert resp.json()["code"] == "connection_error"
        assert_hidden(resp.text)
        assert_logged(audit_events)

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/discovery/databases",
            "/api/v1/discovery/tables",
            "/api/v1/discovery/tables/events/columns",
        ],
    )
    def test_a_query_failure_is_500_without_clickhouses_text(
        self, app, client, admin_headers, audit_events, path
    ):
        failing = SimpleNamespace(query=_refuse)
        app.state.connection_registry = SimpleNamespace(get_client=lambda _name: failing)

        resp = client.get(path, headers=admin_headers)

        assert resp.status_code == 500, resp.text
        assert resp.json()["code"] == "query_error"
        assert_hidden(resp.text)
        assert_logged(audit_events)


class TestOrgs:
    def test_tenant_discovery_hides_clickhouses_text(
        self, app, client, admin_headers, monkeypatch, audit_events
    ):
        def _discovery_fails(*_args, **_kwargs):
            raise _chained(OrgIdDiscoveryError, "cannot read the tenant ids in dfe")

        app.dependency_overrides[get_clickhouse_client] = lambda: object()
        monkeypatch.setattr(orgs_module, "discover_available_org_ids", _discovery_fails)

        resp = client.get("/api/v1/orgs/available-ids", headers=admin_headers)

        assert resp.status_code == 503, resp.text
        assert resp.json()["code"] == "discovery_failed"
        assert_hidden(resp.text)
        assert_logged(audit_events)


class TestSystem:
    def test_a_stored_ttl_clickhouse_refused_says_so_without_its_text(
        self, app, client, admin_headers, monkeypatch, tmp_path, audit_events
    ):
        app.state.gitcrud = GitCrud(
            GitopsRepo(local_path=str(tmp_path / "deploy"), push=False), default_registry()
        )
        monkeypatch.setattr(system_module, "get_clickhouse_client", lambda _settings: object())
        monkeypatch.setattr(system_module, "reconcile_default_ttl", _refuse)

        put = client.put(
            "/api/v1/system/retention", headers=admin_headers, json={"default_ttl_days": 30}
        )
        patch = client.patch(
            "/api/v1/system/defaults", headers=admin_headers, json={"ttl_days": 31}
        )

        for resp in (put, patch):
            assert resp.status_code == 502, resp.text
            assert resp.json()["code"] == "reconcile_failed"
            assert "override stored" in resp.json()["message"]
            assert_hidden(resp.text)
        assert_logged(audit_events)

    def test_unreadable_deployed_tables_hide_clickhouses_text(self, monkeypatch, audit_events):
        def _unreadable(*_args, **_kwargs):
            raise SchemaApplyError(f"could not read ClickHouse state: {SENTINEL}")

        monkeypatch.setattr(system_module, "get_interactive_clickhouse_client", lambda _s: None)
        monkeypatch.setattr(system_module, "live_tables", _unreadable)
        settings = SimpleNamespace(clickhouse=SimpleNamespace(effective_data_database="dfe"))

        with pytest.raises(system_module.HTTPException) as raised:
            system_module._deployed_tables(settings, [SimpleNamespace(deployed_version="1")])

        assert raised.value.status_code == 503
        assert_hidden(str(raised.value.detail))
        assert_logged(audit_events)

    def test_a_cloud_api_failure_hides_its_text_and_keeps_the_engines(self, audit_events):
        transport = system_module._cloud_error(_chained(CloudServiceError, "GET /services failed"))
        refusal = system_module._cloud_error(CloudServiceError("CH Cloud service 'dev' not found"))

        assert transport.status_code == 502
        assert_hidden(str(transport.detail))
        assert refusal.detail["message"] == "CH Cloud service 'dev' not found"
        assert_logged(audit_events)

    def test_a_failed_schema_pass_names_the_stage_not_clickhouses_text(
        self, api_settings, client, viewer_headers, monkeypatch, audit_events
    ):
        def _no_answer(*_args, **_kwargs):
            raise _chained(ManifestApplyError, "ClickHouse did not answer")

        monkeypatch.setattr(phase, "_connect", _no_answer)
        bootstrap = api_settings.model_copy(
            update={
                "clickhouse": api_settings.clickhouse.model_copy(update={"bootstrap_tables": True})
            }
        )
        try:
            state = phase.run_bootstrap(settings=bootstrap, wait_seconds=0)
            resp = client.get("/api/v1/system/schema", headers=viewer_headers)
        finally:
            phase._set_state(phase.SchemaBootstrapState())

        assert state.state == phase.STATE_FAILED
        assert resp.status_code == 200, resp.text
        assert resp.json()["state"] == "failed"
        assert "manifest apply" in resp.json()["error"]
        assert_hidden(resp.text)
        assert_logged(audit_events)

    def test_applying_defaults_reports_clickhouse_without_its_text(self, audit_events):
        source = SimpleNamespace(source="s", table_name="s", deployed_version="2", current="2")
        settings = SimpleNamespace(clickhouse=SimpleNamespace(effective_data_database="dfe"))

        (outcome,) = apply_pinned_defaults(_refuse, settings=settings, sources=[source])

        assert outcome.status == "failed"
        assert outcome.reason == CLICKHOUSE_REFUSED
        assert_logged(audit_events)


class _Admin:
    """A broker admin that refuses whichever call ``refuse`` names."""

    def __init__(self, refuse: str) -> None:
        self._refuse = refuse

    def list_topic_names(self) -> set[str]:
        if self._refuse == "list":
            raise RuntimeError(SENTINEL)
        return set()

    def create(self, *_args, **_kwargs) -> None:
        raise RuntimeError(SENTINEL)


class TestKafkaTopics:
    _SPECS = [TopicSpec(name="acme_land", partitions=3, replication_factor=1)]

    def test_a_refused_create_records_no_broker_text(self, audit_events):
        outcome = ensure_topics(self._SPECS, admin=_Admin("create"))

        assert outcome.failed == [("acme_land", BROKER_REFUSED)]
        assert_logged(audit_events)

    def test_an_unreachable_broker_records_no_broker_text(self, audit_events):
        ensured = ensure_topics(self._SPECS, admin=_Admin("list"))
        status = topic_status(self._SPECS, admin=_Admin("list"))

        assert ensured.failed == [("acme_land", BROKER_UNREACHABLE)]
        assert status.error == BROKER_UNREACHABLE
        assert_logged(audit_events)


class TestSources:
    def test_a_failed_app_reconcile_answers_502_without_the_repos_text(
        self, app, client, admin_headers, monkeypatch, audit_events
    ):
        app.state.gitcrud = object()
        monkeypatch.setattr(sources_module.derived, "plan", _refuse)

        resp = client.post("/api/v1/sources/reconcile-apps", headers=admin_headers)

        assert resp.status_code == 502, resp.text
        assert resp.json()["code"] == "reconcile_failed"
        assert_hidden(resp.text)
        assert_logged(audit_events)

    async def test_a_refused_hyperdx_source_hides_its_text(self, monkeypatch, audit_events):
        from dfe_engine.hyperdx import sources as hyperdx_sources

        async def _refused(*_args, **_kwargs):
            raise RuntimeError(SENTINEL)

        monkeypatch.setattr(hyperdx_sources, "ensure_source", _refused)
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(hyperdx_client=object()))
        )
        source = SimpleNamespace(source="acme", table_name="acme")

        teams, error = await sources_module._sync_hyperdx_source(request, source, "dfe", ["a"])

        assert teams is None
        assert_hidden(error)
        assert_logged(audit_events)

    def test_a_dry_run_hides_why_the_live_table_was_unreadable(self, monkeypatch, audit_events):
        def _unreadable(*_args, **_kwargs):
            raise SchemaApplyError(f"could not read ClickHouse state: {SENTINEL}")

        monkeypatch.setattr(sources_module, "get_interactive_clickhouse_client", lambda _s: None)
        monkeypatch.setattr(sources_module, "live_tables", _unreadable)
        source = SimpleNamespace(source="acme", table_name="acme", deployed_version="1")
        settings = SimpleNamespace(clickhouse=SimpleNamespace(effective_data_database="dfe"))

        change, error = sources_module._dry_run_ttl_change(None, source, "1", None, settings)

        assert change is None
        assert_hidden(error)
        assert_logged(audit_events)

    def test_an_unreadable_clickhouse_on_deploy_is_503_without_its_text(self, audit_events):
        raised = sources_module._ch_unreadable(
            SchemaApplyError(f"could not read ClickHouse state: {SENTINEL}"), "acme"
        )

        assert raised.status_code == 503
        assert raised.detail["code"] == "clickhouse_unavailable"
        assert_hidden(str(raised.detail))
        assert_logged(audit_events)

    def test_a_bulk_failure_keeps_a_refusal_and_hides_the_repos_text(self, audit_events):
        class _Registry:
            def source_exists(self, name: str) -> bool:
                return False

            def delete_source(self, name: str, **_kwargs) -> None:
                if name == "gone":
                    raise SourceNotFoundError("Source 'gone' not found")
                raise RuntimeError(SENTINEL)

        body = SimpleNamespace(action="delete", sources=["acme", "gone"])
        user = AuthContext(user_id="admin")

        _, failed, _ = sources_module._apply_bulk(body, {}, user, _Registry())

        by_source = {entry["source"]: entry for entry in failed}
        assert by_source["gone"]["error"] == "Source 'gone' not found"
        assert by_source["acme"]["code"] == "internal_error"
        assert_hidden(by_source["acme"]["error"])
        assert_logged(audit_events)

    def test_unreadable_telemetry_is_503_without_clickhouses_text(self, audit_events):
        raised = metrics_unavailable(MetricsUnavailableError(f"otel query 'up' failed: {SENTINEL}"))

        assert raised.status_code == 503
        assert raised.detail["code"] == "metrics_unavailable"
        assert_hidden(str(raised.detail))
        assert_logged(audit_events)


class TestSchemas:
    def test_a_json_path_read_clickhouse_refused_hides_its_text(self, audit_events):
        raised = _promotion_refused(
            _chained(JsonPromotionError, "JSON path discovery failed"), "discovery_failed", "acme"
        )

        assert raised.status_code == 422
        assert_hidden(str(raised.detail))
        assert_logged(audit_events)

    def test_a_refused_json_path_keeps_the_engines_message(self):
        raised = _promotion_refused(
            JsonPromotionError("Illegal JSON path: 'a b'"), "discovery_failed", "acme"
        )

        assert raised.detail["message"] == "Illegal JSON path: 'a b'"


class TestSampler:
    def test_an_undescribable_target_hides_clickhouses_text(self, audit_events):
        sampler = Sampler(
            SimpleNamespace(max_execution_time=5),
            None,
            SimpleNamespace(effective_data_database="dfe"),
        )
        ch = SimpleNamespace(query=lambda *_a, **_k: (_ for _ in ()).throw(DatabaseError(SENTINEL)))
        req = SampleRequest(backend=SampleBackend.CLICKHOUSE, table="dfe.events")

        with pytest.raises(SamplerError) as raised:
            sampler.check_org_scope(req, ch, None, ["acme"])

        assert_hidden(str(raised.value))
        assert_logged(audit_events)


class TestOidcCallback:
    def test_a_refused_code_exchange_hides_the_idps_text_from_the_log_as_well(
        self, app, client, audit_events
    ):
        """The exception that reaches the callback can hold the caller's own query string."""

        class _RefusingRp:
            def has_provider(self, provider: str) -> bool:
                return True

            async def handle_callback(self, provider, request):
                raise RuntimeError(SENTINEL)

        app.state.oidc_rp = _RefusingRp()

        resp = client.get("/api/v1/auth/oidc/acme/callback")

        assert resp.status_code == 401, resp.text
        assert_hidden(resp.text)
        (failed,) = [e for e in audit_events if e["event"] == "OIDC callback failed"]
        assert failed["error_type"] == "RuntimeError"
        assert FAKE_HASH not in json.dumps(audit_events, default=str)
