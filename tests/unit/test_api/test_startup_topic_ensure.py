#  Project:      dfe-engine
#  File:         tests/unit/test_api/test_startup_topic_ensure.py
#  Purpose:      The startup topic ensure pass never holds the lifespan on a dead broker
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Startup ensures every carried source's topics in the background, never before it serves.

FastAPI answers nothing, ``/livez`` included, until the lifespan's startup returns,
and with the broker down each admin call waits out its librdkafka timeout. Real
lifespan over a local-only deploy repo, with a broker address nothing listens on.
"""

import secrets
import socket
import time
from pathlib import Path

from confluent_kafka.admin import AdminClient
from fastapi.testclient import TestClient

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries, create_access_token
from dfe_engine.settings import (
    APISettings,
    AuthSettings,
    ClickHouseSettings,
    DFESettings,
    GitopsPackSettings,
    GitopsSettings,
    HuntsSettings,
    KafkaSettings,
    LocalAuthSettings,
    SchemasSettings,
    SecretsSettings,
    ServicesSettings,
    SourceSettings,
)
from tests.support.core_sources import write_landing_definition

# How long TopicAdmin.list_topic_names, the ensure pass's broker call, waits for an answer.
LIST_TOPICS_TIMEOUT_SECONDS = 10.0
NAME = "syslog"
ADMIN_PASSWORD = secrets.token_urlsafe(16)


def _dead_broker() -> str:
    """An address on this host that nothing listens on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"127.0.0.1:{port}"


def _settings(tmp_path: Path, *, ensure_topics: bool | None) -> DFESettings:
    """A bus deployment with a deploy repo, whose broker never answers."""
    for sub in ("sources", "services", "rules", "hunts", "auth", "schemas", "secrets", "orgs"):
        (tmp_path / sub).mkdir(exist_ok=True)
    write_landing_definition(schemas_dir=tmp_path / "schemas")
    return DFESettings(
        env="dev",
        config_dir=str(tmp_path),
        clickhouse=ClickHouseSettings(bootstrap_tables=False),
        kafka=KafkaSettings(bootstrap_servers=_dead_broker(), ensure_topics=ensure_topics),
        source=SourceSettings(sources_dir=str(tmp_path / "sources")),
        services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
        schemas=SchemasSettings(schemas_dir=str(tmp_path / "schemas")),
        hunts=HuntsSettings(rules_dir=str(tmp_path / "rules"), hunt_dir=str(tmp_path / "hunts")),
        auth=AuthSettings(
            enabled=True,
            auth_dir=str(tmp_path / "auth"),
            local=LocalAuthSettings(enabled=True, admin_password=ADMIN_PASSWORD),
        ),
        secrets=SecretsSettings(provider="file", path=str(tmp_path / "secrets")),
        api=APISettings(jwt_secret="test-secret-key-for-unit-tests-hmac32"),
        gitops=GitopsSettings(
            enabled=True,
            local_path=str(tmp_path / "deploy"),
            push=False,
            mode="solo",
            pack=GitopsPackSettings(interval_seconds=3600.0),
        ),
    )


def _carry_a_source(settings: DFESettings) -> None:
    """Write one source into the deploy repo, as a restart finds it."""
    app = create_app(settings=settings)
    token = create_access_token(
        data={"sub": "admin", "org_id": "test-org", "roles": ["admin"]}, settings=settings
    )
    body = {
        "source": NAME,
        "display_name": "Syslog",
        "enabled": True,
        "match": {"field": "tags.collector.type", "value": NAME},
        "header": {"type": "timeseries", "version": "1.0.0"},
    }
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            app.state.account_store.reset_password(new_password=ADMIN_PASSWORD, username="admin")
            created = client.post(
                "/api/v1/sources", json=body, headers={"Authorization": f"Bearer {token}"}
            )
            assert created.status_code == 201, created.text
    finally:
        _registries.clear()


def test_a_dead_broker_never_holds_startup_and_the_ensure_pass_still_runs(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path, ensure_topics=True)
    _carry_a_source(settings)
    # The real librdkafka client in place of the unit tier's guard, so a call waits as it would.
    monkeypatch.setattr("dfe_engine.kafka.topics.AdminClient", AdminClient)
    app = create_app(settings=settings)
    try:
        began = time.monotonic()
        with TestClient(app, raise_server_exceptions=False) as client:
            assert time.monotonic() - began < LIST_TOPICS_TIMEOUT_SECONDS / 2
            assert client.get("/livez").status_code == 200
            ensure = app.state.startup_topic_ensure
            deadline = time.monotonic() + 6 * LIST_TOPICS_TIMEOUT_SECONDS
            while not ensure.done():
                assert time.monotonic() < deadline, "the startup ensure pass never finished"
                time.sleep(0.2)
            result = ensure.result()
    finally:
        _registries.clear()

    assert result is not None
    assert result.created == []
    assert f"{NAME}_land" in [name for name, _reason in result.failed]


def test_left_unset_the_pass_is_never_started(tmp_path: Path) -> None:
    settings = _settings(tmp_path, ensure_topics=None)
    app = create_app(settings=settings)
    try:
        with TestClient(app, raise_server_exceptions=False):
            assert getattr(app.state, "startup_topic_ensure", None) is None
    finally:
        _registries.clear()
