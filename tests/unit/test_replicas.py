#  Project:      dfe-engine
#  File:         tests/unit/test_replicas.py
#  Purpose:      The guard on running more than one engine, and the instance id
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""More than one engine replica boots only on state every replica reaches.

Each refused case must name the setting that holds state on one engine, and the
variable that sets it, so the operator knows what to change.
"""

import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries
from dfe_engine.replicas import UnsharedStateError, instance_id, require_shared_state
from dfe_engine.settings import DFESettings, load_settings, reset_settings

_SHARED = {
    "env": "test",
    "engine_replicas": 2,
    "auth": {"store_backend": "document"},
    "secrets": {"provider": "openbao", "addr": "https://bao.example:8200"},
    "gitops": {"enabled": True, "repo_url": "https://forge.example/acme/deploy.git"},
}


def _settings(**changes: dict | int) -> DFESettings:
    merged: dict = {**_SHARED}
    for section, value in changes.items():
        merged[section] = {**merged[section], **value} if isinstance(value, dict) else value
    return DFESettings.model_validate(merged)


@pytest.mark.parametrize(
    "secrets",
    [
        pytest.param({"provider": "openbao"}, id="openbao"),
        pytest.param({"provider": "vault"}, id="vault"),
        pytest.param(
            {"provider": "aws", "region": "ap-southeast-2", "name_prefix": "acme/dfe/engine"},
            id="aws",
        ),
    ],
)
def test_shared_backends_boot_more_than_one_replica(secrets):
    require_shared_state(_settings(secrets=secrets))


@pytest.mark.parametrize(
    ("changes", "setting", "variable"),
    [
        pytest.param(
            {"auth": {"store_backend": "auto"}},
            "auth.store_backend",
            "DFE_AUTH_STORE_BACKEND",
            id="store-auto",
        ),
        pytest.param(
            {"auth": {"store_backend": "yaml"}},
            "auth.store_backend",
            "DFE_AUTH_STORE_BACKEND",
            id="store-yaml",
        ),
        pytest.param(
            {"secrets": {"provider": "file"}},
            "secrets.provider",
            "DFE_SECRETS_PROVIDER",
            id="secrets-file",
        ),
        pytest.param(
            {"secrets": {"provider": "ansible_vault"}},
            "secrets.provider",
            "DFE_SECRETS_PROVIDER",
            id="secrets-ansible-vault",
        ),
        pytest.param(
            {"gitops": {"enabled": False}}, "gitops.enabled", "DFE_GITOPS_ENABLED", id="gitops-off"
        ),
        pytest.param(
            {"gitops": {"repo_url": ""}},
            "gitops.repo_url",
            "DFE_GITOPS_REPO_URL",
            id="deploy-repo-without-remote",
        ),
        pytest.param(
            {"gitops": {"push": False}}, "gitops.push", "DFE_GITOPS_PUSH", id="deploy-repo-unpushed"
        ),
    ],
)
def test_each_local_backend_is_refused_by_name(changes, setting, variable):
    settings = _settings(**changes)

    with pytest.raises(UnsharedStateError) as refused:
        require_shared_state(settings)

    message = str(refused.value)
    assert setting in message
    assert variable in message
    assert "DFE_ENGINE_REPLICAS=2" in message


def test_every_local_backend_is_named_at_once():
    settings = _settings(
        auth={"store_backend": "yaml"},
        secrets={"provider": "file"},
        gitops={"enabled": False, "repo_url": "", "push": False},
    )

    with pytest.raises(UnsharedStateError) as refused:
        require_shared_state(settings)

    for setting in (
        "auth.store_backend",
        "secrets.provider",
        "gitops.enabled",
        "gitops.repo_url",
        "gitops.push",
    ):
        assert setting in str(refused.value)


@pytest.mark.parametrize("provider", ["gcp", "azure"])
def test_a_provider_the_settings_refuse_is_refused_by_name(monkeypatch, provider):
    monkeypatch.setenv("DFE_ENGINE_REPLICAS", "2")
    monkeypatch.setenv("DFE_SECRETS_PROVIDER", provider)
    reset_settings()
    try:
        with pytest.raises(ValidationError, match=f"DFE_SECRETS_PROVIDER={provider}"):
            load_settings()
    finally:
        reset_settings()


def test_one_replica_keeps_every_local_backend():
    settings = DFESettings(env="test")

    assert settings.engine_replicas == 1
    require_shared_state(settings)


def test_the_replica_count_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("DFE_ENGINE_REPLICAS", "3")
    reset_settings()
    try:
        assert load_settings().engine_replicas == 3
    finally:
        reset_settings()


@pytest.mark.parametrize("count", [0, -1])
def test_fewer_than_one_replica_is_refused(count):
    with pytest.raises(ValidationError, match="engine_replicas"):
        DFESettings(env="test", engine_replicas=count)


def _local(tmp_path: Path, replicas: int) -> DFESettings:
    """A boot on every local backend, with nothing it needs outside *tmp_path*."""
    for sub in ("sources", "services", "rules", "hunts", "auth", "schemas", "secrets"):
        (tmp_path / sub).mkdir(exist_ok=True)
    return DFESettings.model_validate(
        {
            "env": "test",
            "engine_replicas": replicas,
            "config_dir": str(tmp_path),
            "clickhouse": {"bootstrap_tables": False},
            "source": {"sources_dir": str(tmp_path / "sources")},
            "services": {"config_yaml_dir": str(tmp_path / "services")},
            "schemas": {"schemas_dir": str(tmp_path / "schemas")},
            "hunts": {"rules_dir": str(tmp_path / "rules"), "hunt_dir": str(tmp_path / "hunts")},
            "auth": {"auth_dir": str(tmp_path / "auth"), "store_backend": "yaml"},
            "secrets": {"provider": "file", "path": str(tmp_path / "secrets")},
            "api": {"jwt_secret": "test-secret-key-for-unit-tests-hmac32"},
        }
    )


def test_two_replicas_on_local_backends_refuse_to_boot(tmp_path):
    app = create_app(settings=_local(tmp_path, replicas=2))
    try:
        with pytest.raises(UnsharedStateError, match=r"secrets\.provider"):
            with TestClient(app):
                pass
    finally:
        _registries.clear()
    assert not (tmp_path / "auth" / "accounts").exists()


def test_one_replica_on_local_backends_boots(tmp_path):
    app = create_app(settings=_local(tmp_path, replicas=1))
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            assert client.get("/api/v1/auth/setup-status").status_code == 200
    finally:
        _registries.clear()


def test_the_instance_id_is_the_hostname_and_a_boot_uuid():
    assert re.fullmatch(rf"{re.escape(socket.gethostname())}-[0-9a-f]{{32}}", instance_id())


def test_the_instance_id_holds_for_the_life_of_the_process():
    assert instance_id() == instance_id()


def test_another_process_on_the_host_draws_another_instance_id():
    script = "from dfe_engine.replicas import instance_id; print(instance_id())"
    other = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    ).stdout.strip()

    assert other.startswith(f"{socket.gethostname()}-")
    assert other != instance_id()
