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
A listener left unwired accepts no ingest credential at all.
"""

from importlib import resources
from typing import Any

import pytest

from dfe_engine.orgs.ingest_credentials import BEARER_LISTENERS, LISTENER_SECTIONS
from dfe_engine.orgs.ingest_tokens import bearer_secret_path
from dfe_engine.services.models.receiver import ReceiverConfig
from dfe_engine.services.templates import generate_template
from dfe_engine.settings import SecretsSettings
from dfe_engine.yaml_utils import yaml_load_string

# The dfe-receiver config section each listener's auth block sits in.
SECTIONS = {
    "http": "server",
    "grpc": "grpc",
    "otlp": "otlp",
    "splunk_hec": "splunk_hec",
    "prometheus_rw": "prometheus_rw",
}


def _shipped() -> dict[str, Any]:
    path = resources.files("dfe_engine.services") / "default_configs" / "receiver-production.yaml"
    return yaml_load_string(path.read_text(encoding="utf-8"))


CONFIGS = {
    "receiver-production.yaml": _shipped,
    "template:production": lambda: generate_template("receiver", "production"),
}


def test_every_bearer_listener_has_a_section():
    assert set(SECTIONS) == set(BEARER_LISTENERS)
    assert LISTENER_SECTIONS == SECTIONS


@pytest.mark.parametrize("name", sorted(CONFIGS))
@pytest.mark.parametrize("listener", BEARER_LISTENERS)
def test_the_listener_reads_the_secret_the_engine_writes(name: str, listener: str):
    auth = CONFIGS[name]()[SECTIONS[listener]]["auth"]

    assert auth["mode"] == "bearer"
    assert auth["bearer"]["secret_source"] == (
        f"vault:{SecretsSettings().mount}/{bearer_secret_path(listener)}:value"
    )


@pytest.mark.parametrize("name", sorted(CONFIGS))
def test_the_engine_s_receiver_model_takes_it(name: str):
    ReceiverConfig.model_validate(CONFIGS[name]())
