#  Project:      dfe-engine
#  File:         tests/unit/test_kafka_lifecycle.py
#  Purpose:      Managed-Kafka lifecycle tests (WS-C, dfe-engine#99) - all mocked
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Managed-Kafka lifecycle tests (dfe-engine#99) - HTTP/provider layer entirely
MOCKED, no network. Covers:

- ``RedpandaCloudProvider.up`` - create-before-mint-before-acl ordering, the
  derived SASL_SSL/SCRAM-SHA-512 mechanism (never hand-set).
- ``RedpandaCloudProvider.down`` - teardown-to-empty: creds die BEFORE the
  cluster, then the provider asserts the cluster list is empty.
- ``RedpandaCloudProvider.status`` - empty list -> ``is_empty``; a running
  cluster -> id/state/bootstrap.
- ``build_provider`` - confluent-cloud/msk stub cleanly ("not yet"), never an
  ImportError/AttributeError traceback.
- the CLI (``cli/kafka_lifecycle.py``) - ``up`` persists strictly AFTER the
  provider mints creds; ``status`` maps an empty cluster to "$0"/"empty".
- the shared cred-persistence helper (``kafka/cloud/creds.py``) - ``.env``
  upsert is idempotent + non-destructive; secrets go through the SAME
  ``dfe_engine.secrets`` (scalo.secrets) seam the rest of the engine uses.

No live cloud call is made anywhere in this file - every HTTP interaction is a
fake in-process client (``FakeHttpClient`` below), never a socket.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from dfe_engine.kafka import contract
from dfe_engine.kafka.cloud.base import (
    KafkaClusterState,
    KafkaConnection,
    ManagedKafkaProviderError,
)
from dfe_engine.kafka.cloud.creds import forget_connection, persist_connection, upsert_env_file
from dfe_engine.kafka.cloud.redpanda import RedpandaCloudProvider
from dfe_engine.secrets import build_secrets
from dfe_engine.settings import RedpandaCloudSettings, SecretsSettings

AUTH_URL = "https://auth.test/oauth/token"
API_BASE = "https://api.test"
DP_URL = "https://dp.test"
CLUSTER_ID = "c-1"
CLUSTER_NAME = "dfe-kafka"
KAFKA_USER = "dfe-engine"


# --- fake HTTP layer (no sockets, no network) --------------------------------


class _FakeResponse:
    def __init__(self, body: dict) -> None:
        self._body = body

    def json(self) -> dict:
        return self._body


class FakeRedpandaBackend:
    """In-memory emulation of the Redpanda Cloud endpoints the provider calls.

    Records every ``(method, url, kwargs)`` in call order (the ordering IS the
    thing under test) and routes by exact URL match against the fixed test
    constants above - a purpose-built double, not a generic HTTP mock.
    """

    def __init__(self, *, cluster_exists: bool) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self._cluster: dict | None = (
            {
                "id": CLUSTER_ID,
                "name": CLUSTER_NAME,
                "state": "running",
                "dataplane_api": {"url": DP_URL},
                "kafka_api": {"seed_brokers": ["seed.test:9092"]},
            }
            if cluster_exists
            else None
        )
        self.users: set[str] = {KAFKA_USER} if cluster_exists else set()
        self.op_state = "STATE_COMPLETED"

    @property
    def calls_mu(self) -> list[tuple[str, str]]:
        """(method, url) only - the ordering-assertion shape."""
        return [(m, u) for m, u, _ in self.calls]

    def __call__(self, method: str, url: str, kwargs: dict) -> dict:
        self.calls.append((method, url, kwargs))

        if url == AUTH_URL:
            return {"access_token": "test-token"}
        if url == f"{API_BASE}/v1/resource-groups" and method == "get":
            return {"resource_groups": [{"id": "rg-1", "name": "dfe"}]}
        if url == f"{API_BASE}/v1/serverless/clusters" and method == "get":
            return {"serverless_clusters": [self._cluster] if self._cluster else []}
        if url == f"{API_BASE}/v1/serverless/clusters" and method == "post":
            self._cluster = {
                "id": CLUSTER_ID,
                "name": CLUSTER_NAME,
                "state": "running",
                "dataplane_api": {"url": DP_URL},
                "kafka_api": {"seed_brokers": ["seed.test:9092"]},
            }
            return {"operation": {"id": "op-create"}}
        if url.startswith(f"{API_BASE}/v1/serverless/regions"):
            return {"serverless_regions": [{"name": "us-test-1"}]}
        if url == f"{API_BASE}/v1/serverless/clusters/{CLUSTER_ID}" and method == "get":
            assert self._cluster is not None
            return {"serverless_cluster": self._cluster}
        if url == f"{API_BASE}/v1/serverless/clusters/{CLUSTER_ID}" and method == "delete":
            self._cluster = None
            return {"operation": {"id": "op-delete"}}
        if url in (f"{API_BASE}/v1/operations/op-create", f"{API_BASE}/v1/operations/op-delete"):
            return {"operation": {"state": self.op_state}}
        if url == f"{DP_URL}/v1/users" and method == "post":
            self.users.add(KAFKA_USER)
            return {}
        if url == f"{DP_URL}/v1/users/{KAFKA_USER}" and method == "delete":
            self.users.discard(KAFKA_USER)
            return {}
        if url == f"{DP_URL}/v1/acls" and method == "post":
            return {}
        raise AssertionError(f"unexpected call: {method} {url}")


class FakeHttpClient:
    """Context-manager shim over a backend, matching scalo HttpClient's shape
    (``get``/``post``/``delete`` -> an object with ``.json()``)."""

    def __init__(self, backend: FakeRedpandaBackend) -> None:
        self._backend = backend

    def __enter__(self) -> FakeHttpClient:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def get(self, url: str, **kwargs: object) -> _FakeResponse:
        return _FakeResponse(self._backend("get", url, kwargs))

    def post(self, url: str, **kwargs: object) -> _FakeResponse:
        return _FakeResponse(self._backend("post", url, kwargs))

    def delete(self, url: str, **kwargs: object) -> _FakeResponse:
        return _FakeResponse(self._backend("delete", url, kwargs))


class _FakeClock:
    """Injectable clock so operation-polling backstops never really sleep."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def _cfg(**overrides: object) -> RedpandaCloudSettings:
    base = {
        "client_id": "id",
        "client_secret": "secret",
        "auth_url": AUTH_URL,
        "api_base": API_BASE,
        "resource_group": "dfe",
        "cluster_name": CLUSTER_NAME,
        "kafka_user": KAFKA_USER,
    }
    base.update(overrides)
    return RedpandaCloudSettings(**base)


def _provider(backend: FakeRedpandaBackend, **cfg_overrides: object) -> RedpandaCloudProvider:
    clock = _FakeClock()
    return RedpandaCloudProvider(
        _cfg(**cfg_overrides),
        http_factory=lambda: FakeHttpClient(backend),
        sleep=clock.sleep,
        now=clock.now,
    )


# --- RedpandaCloudProvider.up ------------------------------------------------


class TestRedpandaProviderUp:
    def test_creates_cluster_then_mints_creds_then_grants_acls(self) -> None:
        backend = FakeRedpandaBackend(cluster_exists=False)
        _provider(backend).up()

        calls = backend.calls_mu
        cluster_create_idx = calls.index(("post", f"{API_BASE}/v1/serverless/clusters"))
        user_create_idx = calls.index(("post", f"{DP_URL}/v1/users"))
        acl_indices = [i for i, c in enumerate(calls) if c == ("post", f"{DP_URL}/v1/acls")]

        assert cluster_create_idx < user_create_idx, "cluster must be created before user mint"
        assert acl_indices, "expected ACL grant calls"
        assert user_create_idx < min(acl_indices), "user must be minted before ACLs are granted"
        assert len(acl_indices) == 3  # topic + group + cluster

    def test_returns_connection_with_derived_mechanism(self) -> None:
        backend = FakeRedpandaBackend(cluster_exists=False)
        conn = _provider(backend).up()

        expected_protocol, expected_mechanism = contract.derive("redpanda-cloud")
        assert conn.cluster_id == CLUSTER_ID
        assert conn.bootstrap_servers == "seed.test:9092"
        assert conn.security_protocol == expected_protocol
        assert conn.sasl_mechanism == expected_mechanism
        assert conn.username == KAFKA_USER
        assert conn.password
        assert conn.password != ""
        assert conn.extra["http_endpoint"] == DP_URL

    def test_up_is_idempotent_when_cluster_already_exists(self) -> None:
        backend = FakeRedpandaBackend(cluster_exists=True)
        conn = _provider(backend).up()

        calls = backend.calls_mu
        # No create call issued - find-or-create found it already.
        assert ("post", f"{API_BASE}/v1/serverless/clusters") not in calls
        assert conn.cluster_id == CLUSTER_ID

    def test_acl_failure_is_best_effort_not_fatal(self) -> None:
        class FlakyBackend(FakeRedpandaBackend):
            def __call__(self, method: str, url: str, kwargs: dict) -> dict:
                if url == f"{DP_URL}/v1/acls":
                    self.calls.append((method, url, kwargs))
                    raise RuntimeError("simulated ACL endpoint outage")
                return super().__call__(method, url, kwargs)

        backend = FlakyBackend(cluster_exists=False)
        conn = _provider(backend).up()  # must not raise despite every ACL call failing
        assert conn.cluster_id == CLUSTER_ID

    def test_not_configured_raises_before_any_call(self) -> None:
        backend = FakeRedpandaBackend(cluster_exists=False)
        provider = _provider(backend, client_id="", client_secret="")
        with pytest.raises(ManagedKafkaProviderError, match="not configured"):
            provider.up()
        assert backend.calls == []  # never even attempted the token exchange


# --- RedpandaCloudProvider.down ----------------------------------------------


class TestRedpandaProviderDown:
    def test_deletes_creds_before_cluster_then_asserts_empty(self) -> None:
        backend = FakeRedpandaBackend(cluster_exists=True)
        state = _provider(backend).down()

        calls = backend.calls_mu
        user_delete_idx = calls.index(("delete", f"{DP_URL}/v1/users/{KAFKA_USER}"))
        cluster_delete_idx = calls.index(
            ("delete", f"{API_BASE}/v1/serverless/clusters/{CLUSTER_ID}")
        )
        assert user_delete_idx < cluster_delete_idx, (
            "teardown-to-empty is violated: creds must die BEFORE the cluster "
            "(an orphaned key is a support ticket, not a saving)"
        )
        assert state.is_empty is True
        assert KAFKA_USER not in backend.users

    def test_down_on_absent_cluster_is_a_noop_and_reports_empty(self) -> None:
        backend = FakeRedpandaBackend(cluster_exists=False)
        state = _provider(backend).down()
        assert state.is_empty is True
        assert backend.calls_mu == [
            ("post", AUTH_URL),
            ("get", f"{API_BASE}/v1/serverless/clusters"),
        ]

    def test_user_delete_failure_is_best_effort_cluster_still_deleted(self) -> None:
        class FlakyBackend(FakeRedpandaBackend):
            def __call__(self, method: str, url: str, kwargs: dict) -> dict:
                if method == "delete" and url == f"{DP_URL}/v1/users/{KAFKA_USER}":
                    self.calls.append((method, url, kwargs))
                    raise RuntimeError("user already gone (prior partial teardown)")
                return super().__call__(method, url, kwargs)

        backend = FlakyBackend(cluster_exists=True)
        state = _provider(backend).down()
        assert state.is_empty is True  # the cluster delete still ran and completed


# --- RedpandaCloudProvider.status --------------------------------------------


class TestRedpandaProviderStatus:
    def test_empty_cluster_list_maps_to_is_empty(self) -> None:
        backend = FakeRedpandaBackend(cluster_exists=False)
        state = _provider(backend).status()
        assert isinstance(state, KafkaClusterState)
        assert state.is_empty is True
        assert state.exists is False

    def test_running_cluster_reports_id_state_bootstrap(self) -> None:
        backend = FakeRedpandaBackend(cluster_exists=True)
        state = _provider(backend).status()
        assert state.is_empty is False
        assert state.cluster_id == CLUSTER_ID
        assert state.state == "running"
        assert state.bootstrap_servers == "seed.test:9092"


# --- build_provider (confluent-cloud / msk stubs) ----------------------------


class TestBuildProvider:
    def test_confluent_cloud_raises_not_yet(self) -> None:
        from dfe_engine.cli.kafka_lifecycle import build_provider

        with pytest.raises(ManagedKafkaProviderError, match="not yet"):
            build_provider("confluent-cloud")

    def test_msk_raises_aws_layer1_gate(self) -> None:
        from dfe_engine.cli.kafka_lifecycle import build_provider

        with pytest.raises(ManagedKafkaProviderError, match="AWS Layer 1"):
            build_provider("msk")

    def test_unknown_provider_raises(self) -> None:
        from dfe_engine.cli.kafka_lifecycle import build_provider

        with pytest.raises(ManagedKafkaProviderError, match="unknown provider"):
            build_provider("not-a-real-provider")


# --- CLI ordering + output ----------------------------------------------------


class _FakeProvider:
    """A minimal ManagedKafkaProvider stand-in that just records call order."""

    def __init__(self, calls: list[str], *, state: KafkaClusterState | None = None) -> None:
        self._calls = calls
        self._state = state or KafkaClusterState(exists=False)

    def up(self) -> KafkaConnection:
        self._calls.append("provider.up")
        return KafkaConnection(
            cluster_id="c-fake",
            bootstrap_servers="fake:9092",
            security_protocol="SASL_SSL",
            sasl_mechanism="SCRAM-SHA-512",
            username="u",
            password="p",
        )

    def down(self) -> KafkaClusterState:
        self._calls.append("provider.down")
        return self._state

    def status(self) -> KafkaClusterState:
        self._calls.append("provider.status")
        return self._state


class TestCliOrdering:
    def test_up_persists_strictly_after_provider_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import dfe_engine.cli.kafka_lifecycle as mod

        calls: list[str] = []
        monkeypatch.setattr(mod, "build_provider", lambda provider: _FakeProvider(calls))

        def fake_persist(provider: str, conn: KafkaConnection, *, env_path: str) -> None:
            assert conn.cluster_id == "c-fake"  # the minted connection, not a placeholder
            calls.append("persist")

        monkeypatch.setattr(mod, "_persist", fake_persist)

        result = CliRunner().invoke(mod.kafka_lifecycle_app, ["up"])
        assert result.exit_code == 0, result.output
        assert calls == ["provider.up", "persist"], "up must mint creds BEFORE persisting them"

    def test_down_forgets_creds_after_provider_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import dfe_engine.cli.kafka_lifecycle as mod

        calls: list[str] = []
        empty_state = KafkaClusterState(exists=False)
        monkeypatch.setattr(
            mod, "build_provider", lambda provider: _FakeProvider(calls, state=empty_state)
        )
        monkeypatch.setattr(
            "dfe_engine.kafka.cloud.creds.forget_connection",
            lambda **kw: calls.append("forget"),
        )

        result = CliRunner().invoke(mod.kafka_lifecycle_app, ["down"])
        assert result.exit_code == 0, result.output
        assert calls == ["provider.down", "forget"]


class TestCliStatus:
    def test_empty_state_prints_dollar_zero_and_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import dfe_engine.cli.kafka_lifecycle as mod

        monkeypatch.setattr(
            mod,
            "build_provider",
            lambda provider: _FakeProvider([], state=KafkaClusterState(exists=False)),
        )
        result = CliRunner().invoke(mod.kafka_lifecycle_app, ["status"])
        assert result.exit_code == 0, result.output
        assert "$0" in result.output
        assert "empty" in result.output

    def test_running_state_prints_cluster_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import dfe_engine.cli.kafka_lifecycle as mod

        running = KafkaClusterState(
            exists=True, cluster_id="c-1", state="running", bootstrap_servers="b:9092"
        )
        monkeypatch.setattr(
            mod, "build_provider", lambda provider: _FakeProvider([], state=running)
        )
        result = CliRunner().invoke(mod.kafka_lifecycle_app, ["status"])
        assert result.exit_code == 0, result.output
        assert "c-1" in result.output
        assert "running" in result.output

    def test_down_still_present_after_teardown_is_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import dfe_engine.cli.kafka_lifecycle as mod

        still_there = KafkaClusterState(exists=True, cluster_id="c-1", state="running")
        monkeypatch.setattr(
            mod, "build_provider", lambda provider: _FakeProvider([], state=still_there)
        )
        monkeypatch.setattr("dfe_engine.kafka.cloud.creds.forget_connection", lambda **kw: None)

        result = CliRunner().invoke(mod.kafka_lifecycle_app, ["down"])
        assert result.exit_code != 0
        assert "still present" in result.output


# --- shared cred-persistence helper ------------------------------------------


class TestUpsertEnvFile:
    def test_creates_file_when_absent(self, tmp_path) -> None:
        target = tmp_path / ".env"
        upsert_env_file({"DFE_FOO": "bar"}, env_path=target)
        assert target.read_text() == 'DFE_FOO="bar"\n'

    def test_updates_existing_key_in_place_preserves_other_lines(self, tmp_path) -> None:
        target = tmp_path / ".env"
        target.write_text('# a comment\nDFE_FOO="old"\nDFE_UNRELATED="keep-me"\n')
        upsert_env_file({"DFE_FOO": "new"}, env_path=target)
        text = target.read_text()
        assert 'DFE_FOO="new"' in text
        assert "# a comment" in text
        assert 'DFE_UNRELATED="keep-me"' in text
        assert "old" not in text

    def test_appends_new_key_without_touching_existing(self, tmp_path) -> None:
        target = tmp_path / ".env"
        target.write_text('DFE_EXISTING="1"\n')
        upsert_env_file({"DFE_NEW": "2"}, env_path=target)
        text = target.read_text()
        assert 'DFE_EXISTING="1"' in text
        assert 'DFE_NEW="2"' in text


class TestPersistConnection:
    def _secrets_settings(self, tmp_path) -> SecretsSettings:
        return SecretsSettings(provider="file", path=str(tmp_path / "secrets"))

    def test_persist_writes_env_and_secrets_seam(self, tmp_path) -> None:
        env_path = tmp_path / ".env"
        secrets_settings = self._secrets_settings(tmp_path)
        conn = KafkaConnection(
            cluster_id="c-1",
            bootstrap_servers="seed:9092",
            security_protocol="SASL_SSL",
            sasl_mechanism="SCRAM-SHA-512",
            username=KAFKA_USER,
            password="hunter2",
            extra={"http_endpoint": DP_URL},
        )
        persist_connection(
            conn, provider="redpanda-cloud", secrets_settings=secrets_settings, env_path=env_path
        )

        env_text = env_path.read_text()
        assert 'DFE_REDPANDA_KAFKA_USERNAME="dfe-engine"' in env_text
        assert 'DFE_REDPANDA_KAFKA_PASSWORD="hunter2"' in env_text
        assert 'DFE_REDPANDA_BOOTSTRAP_SERVERS="seed:9092"' in env_text
        assert 'DFE_REDPANDA_HTTP_ENDPOINT="https://dp.test"' in env_text

        store = build_secrets(secrets_settings)
        assert store.get("kafka/redpanda-cloud/username") == KAFKA_USER
        assert store.get("kafka/redpanda-cloud/password") == "hunter2"

    def test_forget_deletes_secrets_seam_entries(self, tmp_path) -> None:
        secrets_settings = self._secrets_settings(tmp_path)
        store = build_secrets(secrets_settings)
        store.put("kafka/redpanda-cloud/username", KAFKA_USER)
        store.put("kafka/redpanda-cloud/password", "hunter2")

        forget_connection(provider="redpanda-cloud", secrets_settings=secrets_settings)

        assert store.exists("kafka/redpanda-cloud/username") is False
        assert store.exists("kafka/redpanda-cloud/password") is False
