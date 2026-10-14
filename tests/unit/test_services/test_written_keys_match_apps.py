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
(``tests/support/producer_contract.py``) is the app's contract. Three writers are
held to it: ``stored_form``, which every save goes through; ``generate_template``
for each profile; and the shipped ``default_configs`` that ``seed_defaults``
copies byte for byte.
"""

import json
import types
import typing
from importlib import resources
from typing import Any

import pytest
from pydantic import BaseModel

from dfe_engine.services.models.loader import LoaderConfig
from dfe_engine.services.models.receiver import ReceiverConfig
from dfe_engine.services.models.transform_vector import TransformVectorConfig
from dfe_engine.services.models.transform_vrl import TransformVrlConfig
from dfe_engine.services.registry import stored_form
from dfe_engine.services.templates import generate_template
from dfe_engine.yaml_utils import yaml_load_string
from tests.support.producer_contract import (
    DFE_LOADER,
    DFE_RECEIVER,
    DFE_TRANSFORM_VECTOR,
    DFE_TRANSFORM_VRL,
    Producer,
    producer_file,
)

_SCHEMA = "docs/config-schema.json"
_PROFILES = ("default", "production", "k8s")

# service name -> (its model, the app that reads what the engine writes for it)
_APPS: dict[str, tuple[type[BaseModel], Producer]] = {
    "loader": (LoaderConfig, DFE_LOADER),
    "receiver": (ReceiverConfig, DFE_RECEIVER),
    "transform-vrl": (TransformVrlConfig, DFE_TRANSFORM_VRL),
    "transform-vector": (TransformVectorConfig, DFE_TRANSFORM_VECTOR),
}

# Keys the engine writes that the app's config struct has no field for; the set is
# exact, so a new one fails and so does one that is fixed. transform-vrl's metrics
# and scaling are scalo cascade sections, read outside that struct from a --config file.
_UNREAD: dict[str, frozenset[str]] = {
    "loader": frozenset({"memory.tracking_enabled", "metrics.path"}),
    "receiver": frozenset(
        {
            "kafka.producer",
            "kafka.sasl.aws_access_key_id",
            "kafka.sasl.aws_profile",
            "kafka.sasl.aws_region",
            "kafka.sasl.aws_secret_access_key",
            "kafka.sasl.aws_session_token",
            "kafka.sasl.oauth_client_id",
            "kafka.sasl.oauth_client_secret",
            "kafka.sasl.oauth_extensions",
            "kafka.sasl.oauth_scope",
            "kafka.sasl.oauth_token_endpoint",
            "kafka.tls.skip_verify",
            "logging",
            "metrics.path",
            "validation.require_json",
        }
    ),
    "transform-vrl": frozenset(
        {
            "health",
            "logging",
            "metrics",
            "scaling",
            *(
                f"{side}.sasl.{key}"
                for side in ("source", "sink")
                for key in (
                    "aws_access_key_id",
                    "aws_profile",
                    "aws_region",
                    "aws_secret_access_key",
                    "aws_session_token",
                    "oauth_client_id",
                    "oauth_client_secret",
                    "oauth_extensions",
                    "oauth_scope",
                    "oauth_token_endpoint",
                )
            ),
        }
    ),
    # dfe-transform-vector reads source/sink/pipeline/vector blocks; the engine's
    # model is shaped for a kafka/sources layout the app does not have.
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


def _model_in(annotation: Any) -> type[BaseModel] | None:
    """The one model a field holds, bare or optional; None for a list, map or scalar."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    if typing.get_origin(annotation) in (types.UnionType, typing.Union):
        models = [
            arg
            for arg in typing.get_args(annotation)
            if isinstance(arg, type) and issubclass(arg, BaseModel)
        ]
        if len(models) == 1:
            return models[0]
    return None


def _every_block(model: type[BaseModel]) -> dict[str, Any]:
    """Input that sets every nested block ``model`` declares, the optional ones too."""
    doc: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        nested = _model_in(field.annotation)
        if nested is not None:
            doc[field.alias or name] = _every_block(nested)
    return doc


def _variants(node: Any, defs: dict[str, Any]) -> list[dict[str, Any]]:
    """The schemas a value at ``node`` may match, references and unions expanded."""
    if not isinstance(node, dict):
        return []
    while "$ref" in node:
        node = defs[node["$ref"].rsplit("/", 1)[-1]]
    found: list[dict[str, Any]] = []
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in node.get(key, []):
            found.extend(_variants(sub, defs))
    return found or [node]


def _unread_keys(value: Any, node: Any, defs: dict[str, Any], path: str = "") -> list[str]:
    """Every key path in ``value`` that the schema ``node`` gives no field for."""
    variants = _variants(node, defs)
    if isinstance(value, list):
        items = [v["items"] for v in variants if isinstance(v.get("items"), dict)]
        if not items:
            return []
        return [key for item in value for key in _unread_keys(item, items[0], defs, f"{path}[]")]
    if not isinstance(value, dict):
        return []
    structs = [v for v in variants if "properties" in v or "additionalProperties" in v]
    # No struct to compare against, or one that takes any key: nothing is unread.
    if not structs or any(v.get("additionalProperties") is True for v in structs):
        return []
    fields: dict[str, Any] = {}
    entries: dict[str, Any] | None = None
    for struct in structs:
        fields.update(struct.get("properties", {}))
        if isinstance(struct.get("additionalProperties"), dict):
            entries = struct["additionalProperties"]
    unread: list[str] = []
    for key, sub in value.items():
        where = f"{path}.{key}" if path else key
        if key in fields:
            unread.extend(_unread_keys(sub, fields[key], defs, where))
        elif entries is not None:
            unread.extend(_unread_keys(sub, entries, defs, where))
        else:
            unread.append(where)
    return unread


@pytest.fixture(scope="module")
def schemas() -> dict[str, dict[str, Any]]:
    """Each app's config schema at its pinned release, keyed by engine service name."""
    found: dict[str, dict[str, Any]] = {}
    for service, (_, producer) in _APPS.items():
        source = producer_file(producer, _SCHEMA)
        schema = json.loads(source.text)
        # A reshaped schema must fail here, not pass every walk against nothing.
        assert schema.get("properties"), f"no top-level fields in {source.origin}"
        found[service] = schema
    return found


def _written(service: str) -> list[tuple[str, dict[str, Any]]]:
    """What each writer puts in a config file for ``service``, named by writer."""
    model, _ = _APPS[service]
    docs = [("stored_form", stored_form(model.model_validate(_every_block(model))))]
    docs += [(f"template:{p}", generate_template(service, p)) for p in _PROFILES]
    root = resources.files("dfe_engine.services") / "default_configs"
    for entry in sorted(root.iterdir(), key=lambda e: e.name):
        if entry.name.startswith(f"{service}-") and entry.name.endswith(".yaml"):
            docs.append((entry.name, yaml_load_string(entry.read_text(encoding="utf-8")) or {}))
    return docs


_WRITERS = [(service, writer) for service in _APPS for writer, _ in _written(service)]


def _unread_in(doc: dict[str, Any], schema: dict[str, Any]) -> set[str]:
    defs = schema.get("$defs", schema.get("definitions", {}))
    return set(_unread_keys(doc, schema, defs))


def _unread_by(service: str, writer: str, schema: dict[str, Any]) -> set[str]:
    return _unread_in(dict(_written(service))[writer], schema)


@pytest.mark.parametrize(("service", "writer"), _WRITERS, ids=[f"{s}-{w}" for s, w in _WRITERS])
def test_every_written_key_is_one_the_app_reads(schemas, service: str, writer: str):
    unread = _unread_by(service, writer, schemas[service]) - _UNREAD[service]
    assert not unread, (
        f"{writer} writes {sorted(unread)} for the {service}, which has no such "
        "field: serde drops it and the app runs on its own default"
    )


@pytest.mark.parametrize("service", sorted(_APPS))
def test_the_unread_set_is_exact(schemas, service: str):
    writers = [writer for s, writer in _WRITERS if s == service]
    seen = set().union(*(_unread_by(service, w, schemas[service]) for w in writers))
    assert seen == _UNREAD[service], (
        f"no longer written or now read by the {service}: {sorted(_UNREAD[service] - seen)}; "
        f"newly unread: {sorted(seen - _UNREAD[service])}"
    )


def test_the_loader_reads_its_ca_where_the_engine_writes_it(schemas):
    written = stored_form(LoaderConfig.model_validate({"kafka": {"tls": {"ca_file": "/ca.pem"}}}))
    assert written["kafka"]["tls"]["ca_cert_file"] == "/ca.pem"
    assert "ca_file" not in written["kafka"]["tls"]
    assert not _unread_in(written, schemas["loader"]) - _UNREAD["loader"]


@pytest.mark.parametrize("side", ["source", "sink"])
def test_transform_vrl_reads_its_ca_where_the_engine_writes_it(schemas, side: str):
    config = TransformVrlConfig.model_validate({side: {"tls": {"ca_file": "/ca.pem"}}})
    written = stored_form(config)
    assert written[side]["tls"]["ca_cert_file"] == "/ca.pem"
    assert "ca_file" not in written[side]["tls"]
    assert not _unread_in(written, schemas["transform-vrl"]) - _UNREAD["transform-vrl"]
