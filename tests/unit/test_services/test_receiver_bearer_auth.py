#  Project:      dfe-engine
#  File:         tests/unit/test_services/test_receiver_bearer_auth.py
#  Purpose:      Every receiver listener that takes a bearer token reads the engine's secret for it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The production receiver config points each bearer listener at the secret the engine writes.

dfe-receiver authenticates bearer tokens on the HTTP, gRPC, OTLP, Splunk HEC and
Prometheus Remote Write listeners, each from its own ``auth.bearer.secret_source``.
A listener left unwired accepts no ingest credential at all. The generated production
template and the receiver-production.yaml a seed writes both name the engine's own
secrets backend.
"""

from pathlib import Path
from typing import Any

import pytest

from dfe_engine.orgs.ingest_credentials import BEARER_LISTENERS, LISTENER_SECTIONS
from dfe_engine.orgs.ingest_tokens import (
    BEARER_REFRESH_SECONDS,
    BearerSourceUnreadableError,
    bearer_secret_path,
)
from dfe_engine.services.models.receiver import ReceiverConfig
from dfe_engine.services.registry import ServiceConfigRegistry
from dfe_engine.services.templates import generate_template
from dfe_engine.settings import DFESettings, SecretsSettings
from dfe_engine.yaml_utils import yaml_load_string

# The dfe-receiver config section each listener's auth block sits in.
SECTIONS = {
    "http": "server",
    "grpc": "grpc",
    "otlp": "otlp",
    "splunk_hec": "splunk_hec",
    "prometheus_rw": "prometheus_rw",
}

BACKENDS = {
    "aws": SecretsSettings(provider="aws", region="ap-southeast-2", name_prefix="dfe/dev/engine"),
    "openbao": SecretsSettings(provider="openbao", addr="https://bao:8200", mount="kv-dfe"),
}

# Each listener's secret_source on each backend, with {path} the secret the engine writes.
SOURCES = {
    "aws": "aws:dfe/dev/engine/{path}",
    "openbao": "vault:kv-dfe/{path}:value",
}


def _seeded(directory: Path, settings: DFESettings | None = None) -> dict[str, Any]:
    """The receiver-production.yaml a seed writes into *directory*, read back."""
    registry = ServiceConfigRegistry(config_directory=directory, refresh_interval=0)
    try:
        if settings is None:
            registry.seed_defaults()
        else:
            registry.seed_defaults(settings=settings)
    finally:
        registry.close()
    return yaml_load_string((directory / "receiver-production.yaml").read_text(encoding="utf-8"))


def _bearer(backend: str, listener: str) -> dict[str, Any]:
    source = SOURCES[backend].format(path=bearer_secret_path(listener))
    return {
        "mode": "bearer",
        "bearer": {"secret_source": source, "refresh_interval_secs": BEARER_REFRESH_SECONDS},
    }


@pytest.fixture
def engine_on(monkeypatch):
    """Run the process on a secrets backend, as an engine booted with DFE_SECRETS_PROVIDER is."""

    def on(backend: SecretsSettings) -> DFESettings:
        settings = DFESettings(env="dev", secrets=backend)
        monkeypatch.setattr("dfe_engine.settings._settings", settings)
        return settings

    return on


def test_every_bearer_listener_has_a_section():
    assert set(SECTIONS) == set(BEARER_LISTENERS)
    assert LISTENER_SECTIONS == SECTIONS


@pytest.mark.parametrize("backend", sorted(BACKENDS))
@pytest.mark.parametrize("listener", BEARER_LISTENERS)
def test_the_seeded_production_config_reads_the_secret_on_the_engine_s_backend(
    engine_on, tmp_path, backend: str, listener: str
):
    engine_on(BACKENDS[backend])

    assert _seeded(tmp_path)[SECTIONS[listener]]["auth"] == _bearer(backend, listener)


def test_a_seed_builds_from_the_settings_it_is_given(engine_on, tmp_path):
    engine_on(BACKENDS["openbao"])
    settings = DFESettings(env="dev", secrets=BACKENDS["aws"])

    assert _seeded(tmp_path, settings)["server"]["auth"] == _bearer("aws", "http")


def test_a_backend_the_receiver_cannot_read_refuses_the_seed(engine_on, tmp_path):
    engine_on(SecretsSettings(provider="ansible_vault"))

    with pytest.raises(BearerSourceUnreadableError, match="DFE_SECRETS_PROVIDER=ansible_vault"):
        _seeded(tmp_path)


@pytest.mark.parametrize("backend", sorted(BACKENDS))
@pytest.mark.parametrize("listener", BEARER_LISTENERS)
def test_the_production_template_reads_the_secret_on_the_engine_s_backend(
    engine_on, backend: str, listener: str
):
    engine_on(BACKENDS[backend])

    auth = generate_template("receiver", "production")[SECTIONS[listener]]["auth"]

    assert auth["mode"] == "bearer"
    assert auth["bearer"]["secret_source"] == SOURCES[backend].format(
        path=bearer_secret_path(listener)
    )
    # The interval the API promises a revoked token stops within, as the compile sets it.
    assert auth["bearer"]["refresh_interval_secs"] == BEARER_REFRESH_SECONDS


def test_the_template_reads_the_backend_when_generated_not_when_the_plugin_loads(engine_on):
    engine_on(BACKENDS["openbao"])
    first = generate_template("receiver", "production")["server"]["auth"]["bearer"]
    engine_on(BACKENDS["aws"])
    second = generate_template("receiver", "production")["server"]["auth"]["bearer"]

    assert first["secret_source"].startswith("vault:")
    assert second["secret_source"].startswith("aws:")


def test_a_backend_the_receiver_cannot_read_refuses_the_template(engine_on):
    engine_on(SecretsSettings(provider="ansible_vault"))

    with pytest.raises(BearerSourceUnreadableError, match="DFE_SECRETS_PROVIDER=ansible_vault"):
        generate_template("receiver", "production")


def test_the_other_profiles_read_no_backend(engine_on):
    engine_on(SecretsSettings(provider="ansible_vault"))

    for profile in ("default", "k8s"):
        generate_template("receiver", profile)


@pytest.mark.parametrize("backend", sorted(BACKENDS))
def test_the_seeded_config_is_one_the_receiver_model_takes(engine_on, tmp_path, backend: str):
    engine_on(BACKENDS[backend])
    ReceiverConfig.model_validate(_seeded(tmp_path))


@pytest.mark.parametrize("backend", sorted(BACKENDS))
def test_the_template_is_one_the_receiver_model_takes(engine_on, backend: str):
    engine_on(BACKENDS[backend])
    ReceiverConfig.model_validate(generate_template("receiver", "production"))


def test_the_seed_carries_only_the_production_keys_and_leaves_the_rest_to_the_receiver(
    engine_on, tmp_path
):
    """The loader address and listener ports follow the manifest and the receiver, not the seed."""
    engine_on(BACKENDS["openbao"])
    seeded = _seeded(tmp_path)

    assert "loader" not in seeded
    assert "bind_address" not in seeded["server"]
    assert "bind_address" not in seeded.get("grpc", {})
