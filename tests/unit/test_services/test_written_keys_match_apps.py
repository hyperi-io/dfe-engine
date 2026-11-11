#  Project:      dfe-engine
#  File:         tests/unit/test_services/test_written_keys_match_apps.py
#  Purpose:      Every key the engine writes for an app is one the app reads
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Every key the engine writes for an app, at any depth, is one the app reads.

serde drops an unknown key without a word, so a key spelt the engine's way rather
than the app's reads as set while the app runs on its own default: a CA file
written as ``ca_file`` left the loader verifying against the system trust store.

Each app generates ``docs/config-schema.json`` from its config structs and fails
its own build when the committed copy drifts, so that file at the pinned release
(``tests/support/producer_contract.py``) is the app's contract. Every config
``seed_defaults`` writes, from ``default_configs``, is held to it. A save writes
only what the operator sent, judged against the mounted copy of the same file.
"""

import json
from typing import Any

import pytest

from dfe_engine.services.registry import default_configs
from dfe_engine.settings import DFESettings, SecretsSettings
from dfe_engine.yaml_utils import yaml_load_string
from tests.support.config_schema import definitions, type_mismatches, unread_keys
from tests.support.producer_contract import (
    DFE_LOADER,
    DFE_RECEIVER,
    DFE_TRANSFORM_VECTOR,
    Producer,
    producer_file,
)

_SCHEMA = "docs/config-schema.json"
# A backend the receiver reads, so the seeds do not hang on a developer .env.
_ENGINE = DFESettings(env="dev", secrets=SecretsSettings(provider="openbao"))

# service name -> the app that reads what the engine writes for it
_APPS: dict[str, Producer] = {
    "loader": DFE_LOADER,
    "receiver": DFE_RECEIVER,
    "transform-vector": DFE_TRANSFORM_VECTOR,
}

# Keys the engine writes that the app's config struct has no field for; the set is
# exact, so a new one fails and so does one that is fixed.
_UNREAD: dict[str, frozenset[str]] = {
    "loader": frozenset(),
    "receiver": frozenset(),
    # dfe-transform-vector reads source/sink/pipeline/vector blocks; the shipped seed
    # is shaped for a kafka/sources layout the app does not have.
    "transform-vector": frozenset(
        {
            "extra_env",
            "extra_yaml_files",
            "ipc",
            "kafka",
            "memory",
            "metrics.enabled",
            "metrics.path",
            "sources",
        }
    ),
}


@pytest.fixture(scope="module")
def schemas() -> dict[str, dict[str, Any]]:
    """Each app's config schema at its pinned release, keyed by engine service name."""
    found: dict[str, dict[str, Any]] = {}
    for service, producer in _APPS.items():
        source = producer_file(producer, _SCHEMA)
        schema = json.loads(source.text)
        # A reshaped schema must fail here, not pass every walk against nothing.
        assert schema.get("properties"), f"no top-level fields in {source.origin}"
        found[service] = schema
    return found


def _written(service: str) -> dict[str, dict[str, Any]]:
    """Every config the seed writes for ``service``, by file name."""
    return {
        name: yaml_load_string(text) or {}
        for name, text in default_configs(_ENGINE).items()
        if name.startswith(f"{service}-")
    }


_WRITERS = [(service, name) for service in _APPS for name in _written(service)]


def _unread_by(service: str, name: str, schema: dict[str, Any]) -> set[str]:
    return set(unread_keys(_written(service)[name], schema, definitions(schema)))


@pytest.mark.parametrize(("service", "name"), _WRITERS, ids=[f"{s}-{n}" for s, n in _WRITERS])
def test_every_written_key_is_one_the_app_reads(schemas, service: str, name: str):
    unread = _unread_by(service, name, schemas[service]) - _UNREAD[service]
    assert not unread, (
        f"{name} writes {sorted(unread)} for the {service}, which has no such "
        "field: serde drops it and the app runs on its own default"
    )


@pytest.mark.parametrize(("service", "name"), _WRITERS, ids=[f"{s}-{n}" for s, n in _WRITERS])
def test_every_written_value_is_of_a_type_the_app_takes(schemas, service: str, name: str):
    schema = schemas[service]
    assert type_mismatches(_written(service)[name], schema, definitions(schema)) == []


@pytest.mark.parametrize("service", sorted(_APPS))
def test_the_unread_set_is_exact(schemas, service: str):
    names = [name for s, name in _WRITERS if s == service]
    seen = set().union(*(_unread_by(service, n, schemas[service]) for n in names))
    assert seen == _UNREAD[service], (
        f"no longer written or now read by the {service}: {sorted(_UNREAD[service] - seen)}; "
        f"newly unread: {sorted(seen - _UNREAD[service])}"
    )


def test_no_loader_config_the_engine_writes_carries_coercion():
    """dfe-loader retired its coercion section, so the app's own handling decides."""
    written = _written("loader")
    assert written
    assert [name for name, doc in written.items() if "coercion" in doc] == []
