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
template names the engine's own secrets backend. The shipped receiver-production.yaml
is a fixed reference for an OpenBao deployment.
"""

from importlib import resources
from typing import Any

import pytest

from dfe_engine.orgs.ingest_credentials import BEARER_LISTENERS, LISTENER_SECTIONS
from dfe_engine.orgs.ingest_tokens import (
    BEARER_REFRESH_SECONDS,
    BearerSourceUnreadableError,
    bearer_secret_path,
)
from dfe_engine.services.models.receiver import ReceiverConfig
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


def _shipped() -> dict[str, Any]:
    path = resources.files("dfe_engine.services") / "default_configs" / "receiver-production.yaml"
    return yaml_load_string(path.read_text(encoding="utf-8"))


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


@pytest.mark.parametrize("listener", BEARER_LISTENERS)
def test_the_shipped_reference_reads_the_openbao_secret(listener: str):
    auth = _shipped()[SECTIONS[listener]]["auth"]

    assert auth["mode"] == "bearer"
    assert auth["bearer"]["secret_source"] == (
        f"vault:{SecretsSettings().mount}/{bearer_secret_path(listener)}:value"
    )
    assert auth["bearer"]["refresh_interval_secs"] == BEARER_REFRESH_SECONDS


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


def test_the_shipped_reference_is_one_the_receiver_model_takes():
    ReceiverConfig.model_validate(_shipped())


@pytest.mark.parametrize("backend", sorted(BACKENDS))
def test_the_template_is_one_the_receiver_model_takes(engine_on, backend: str):
    engine_on(BACKENDS[backend])
    ReceiverConfig.model_validate(generate_template("receiver", "production"))
