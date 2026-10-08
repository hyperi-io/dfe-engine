#  Project:      dfe-engine
#  File:         tests/unit/test_services/test_delivery_keys.py
#  Purpose:      The service mirrors accept the delivery keys the apps now read
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Every block that holds its acknowledgement takes the key, and its default is the app's.

The mirrors forbid unknown keys, so a key the app reads and the mirror lacks is
refused on the /services path before it reaches anything. The defaults are
compared with the schemas the apps emit (``tests/fixtures/contract-acknowledgements``,
copied byte for byte from each app's ``docs/config-schema.json`` but for the secret
marker, which reads ``x-scalo-secret``), not with a number typed in here.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from dfe_engine.services.models.archiver import ArchiverConfig
from dfe_engine.services.models.loader import LoaderConfig
from dfe_engine.services.models.receiver import ReceiverConfig
from dfe_engine.services.models.transform_vrl import TransformVrlConfig
from dfe_engine.services.validators import validate_config

SCHEMAS = Path(__file__).parents[2] / "fixtures" / "contract-acknowledgements"

# (plugin name, mirror, top-level block, the app whose schema it mirrors)
HOLDING_BLOCKS = [
    ("receiver", ReceiverConfig, "server", "dfe-receiver"),
    ("receiver", ReceiverConfig, "grpc", "dfe-receiver"),
    ("loader", LoaderConfig, "kafka", "dfe-loader"),
    ("loader", LoaderConfig, "grpc", "dfe-loader"),
    ("transform-vrl", TransformVrlConfig, "source", "dfe-transform-vrl"),
    ("archiver", ArchiverConfig, "kafka", "dfe-archiver"),
]

# The least each plugin's cross-field check accepts, so a failure names the new key.
BASE: dict[str, dict[str, Any]] = {
    "receiver": {"kafka": {"brokers": ["kafka:9092"]}},
    "loader": {},
    "transform-vrl": {"sink": {"topic": "main_load"}, "transforms": {"dir": "/etc/vrl"}},
    "archiver": {"kafka": {"topics": ["main_land"]}},
}


def _schema(app: str) -> dict:
    return json.loads((SCHEMAS / app / "config-schema.json").read_text())


def _app_default(app: str, dotted: str) -> Any:
    """The default the app's schema gives a key, read off its top-level default block."""
    schema = _schema(app)
    section, _, rest = dotted.partition(".")
    node: Any = schema["properties"][section]["default"]
    for part in rest.split("."):
        node = node[part]
    return node


class TestAcknowledgements:
    @pytest.mark.parametrize(("service", "model", "block", "app"), HOLDING_BLOCKS)
    def test_turning_it_off_validates(self, service, model, block, app):
        base = BASE[service]
        doc = {**base, block: {**base.get(block, {}), "acknowledgements": {"enabled": False}}}
        result = validate_config(service, doc)
        assert result.valid, result.errors
        assert getattr(model.model_validate(doc), block).acknowledgements.enabled is False

    @pytest.mark.parametrize(("service", "model", "block", "app"), HOLDING_BLOCKS)
    def test_the_default_is_the_app_s(self, service, model, block, app):
        mirrored = getattr(model(), block).acknowledgements.model_dump()
        assert mirrored == _app_default(app, f"{block}.acknowledgements")
        assert mirrored == {"enabled": True}

    @pytest.mark.parametrize(("service", "model", "block", "app"), HOLDING_BLOCKS)
    def test_a_misspelt_key_inside_it_is_refused(self, service, model, block, app):
        # extra="forbid" is what catches a typo the app would drop without a word.
        with pytest.raises(ValidationError):
            model.model_validate({block: {"acknowledgements": {"enable": False}}})

    @pytest.mark.parametrize(("service", "model", "block", "app"), HOLDING_BLOCKS)
    def test_a_non_boolean_is_refused(self, service, model, block, app):
        with pytest.raises(ValidationError):
            model.model_validate({block: {"acknowledgements": {"enabled": "sometimes"}}})


class TestTheKeysBesideIt:
    def test_the_receiver_grpc_listener_takes_a_message_size(self):
        doc = {**BASE["receiver"], "grpc": {"max_message_size": 4 * 1024 * 1024}}
        result = validate_config("receiver", doc)
        assert result.valid, result.errors
        assert ReceiverConfig.model_validate(doc).grpc.max_message_size == 4 * 1024 * 1024

    def test_the_receiver_grpc_message_size_default_is_the_app_s(self):
        assert ReceiverConfig().grpc.max_message_size == _app_default(
            "dfe-receiver", "grpc.max_message_size"
        )

    def test_the_loader_push_hold_default_is_the_app_s(self):
        assert LoaderConfig().grpc.max_hold_ms == _app_default("dfe-loader", "grpc.max_hold_ms")

    def test_a_negative_hold_is_refused(self):
        with pytest.raises(ValidationError):
            LoaderConfig.model_validate({"grpc": {"max_hold_ms": -1}})
