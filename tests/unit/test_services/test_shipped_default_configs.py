#  Project:      dfe-engine
#  File:         tests/unit/test_services/test_shipped_default_configs.py
#  Purpose:      The config blobs the engine ships have to be ones the apps accept
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Every config the engine seeds is checked against what the app will take.

These files are authored here and read by a Rust app, so nothing in the engine's
own test suite catches a key the app ignores or refuses. Two things are asserted:
the blob validates against its service model, and the archiver's path template
carries only placeholders dfe-archiver substitutes -- it refuses to start on any
other token, so a shipped default with one strands the deployment.
"""

import json
import re
from pathlib import Path

import pytest

from dfe_engine.appmgmt import catalogue
from dfe_engine.services.models.archiver import PATH_TEMPLATE_PLACEHOLDERS
from dfe_engine.services.plugins import get_plugin
from dfe_engine.services.registry import ServiceConfigRegistry
from dfe_engine.yaml_utils import yaml_load_string

_TOKEN = re.compile(r"\{[^{}]*\}")


def _shipped_configs() -> list[tuple[str, str]]:
    from importlib import resources

    root = resources.files("dfe_engine.services") / "default_configs"
    return sorted(
        (entry.name, entry.read_text(encoding="utf-8"))
        for entry in root.iterdir()
        if entry.name.endswith(".yaml")
    )


_SHIPPED = _shipped_configs()
_ARCHIVER = [(name, text) for name, text in _SHIPPED if name.startswith("archiver-")]
_LOADER = [(name, text) for name, text in _SHIPPED if name.startswith("loader-")]
_RECEIVER = [(name, text) for name, text in _SHIPPED if name.startswith("receiver-")]

_FIXTURES = Path(__file__).parents[2] / "fixtures"


def _app_default(fixture_dir: str, app: str, section: str) -> dict:
    """The default an app's emitted schema gives one top-level section."""
    schema = json.loads((_FIXTURES / fixture_dir / app / "config-schema.json").read_text())
    return schema["properties"][section]["default"]


@pytest.mark.parametrize(("name", "text"), _SHIPPED, ids=[n for n, _ in _SHIPPED])
def test_every_shipped_config_validates_against_its_service_model(name: str, text: str):
    """The blob parses into the service's model -- no refused value, no unknown key.

    The model layer only. The production blobs leave credentials blank for the
    deploy layer to fill, so the cross-field completeness rules do not apply to
    a template the way they apply to a deployment's own config.
    """
    parsed = ServiceConfigRegistry._parse_table_name(name.removesuffix(".yaml"))
    assert parsed is not None, f"{name} does not name a service"
    service, _instance = parsed

    get_plugin(service).config_class.model_validate(yaml_load_string(text) or {})


@pytest.mark.parametrize(("name", "text"), _ARCHIVER, ids=[n for n, _ in _ARCHIVER])
def test_every_shipped_archiver_template_uses_a_placeholder_the_archiver_substitutes(
    name: str, text: str
):
    # dfe-archiver PR #73: the loader rejects an unknown token at startup, so
    # {topic} or {date} here is a pod that never boots.
    template = (yaml_load_string(text) or {})["archive"]["path_template"]
    unsupported = set(_TOKEN.findall(template)) - PATH_TEMPLATE_PLACEHOLDERS

    assert unsupported == set(), f"{name} carries {sorted(unsupported)}"


@pytest.mark.parametrize(("name", "text"), _ARCHIVER, ids=[n for n, _ in _ARCHIVER])
def test_no_shipped_archiver_config_pins_the_roll_interval(name: str, text: str):
    # dfe-archiver #98 picks the interval from whether it holds offsets; a pin overrides that.
    archive = (yaml_load_string(text) or {})["archive"]
    assert "roll_interval_secs" not in archive, f"{name} pins archive.roll_interval_secs"


@pytest.mark.parametrize(("name", "text"), _LOADER, ids=[n for n, _ in _LOADER])
@pytest.mark.parametrize("field", ["group", "client_id", "topics"])
def test_every_shipped_loader_config_consumes_as_the_loader_does(name: str, text: str, field: str):
    # A seeded [events] had the loader read one topic nothing writes, where the
    # loader's own empty list discovers every *_load / *_land topic.
    shipped = (yaml_load_string(text) or {})["kafka"][field]
    assert shipped == _app_default("contract", "dfe-loader", "kafka")[field], name


@pytest.mark.parametrize(("name", "text"), _RECEIVER, ids=[n for n, _ in _RECEIVER])
def test_every_shipped_receiver_config_sends_inside_the_hold(name: str, text: str):
    # A seeded 5 s deadline replaced the receiver's own 20 s one.
    shipped = (yaml_load_string(text) or {})["loader"]["timeout_ms"]
    app = _app_default("contract-acknowledgements", "dfe-receiver", "loader")["timeout_ms"]
    assert shipped == app, name


@pytest.mark.parametrize(("name", "text"), _RECEIVER, ids=[n for n, _ in _RECEIVER])
def test_no_shipped_receiver_config_accepts_a_published_header(name: str, text: str):
    # dfe-receiver #173: x-hyperi-agent: 1.0 is public, so it admits nobody.
    auth = (yaml_load_string(text) or {})["server"]["auth"]
    assert auth.get("accepted_headers", []) == [], name


def test_the_receiver_model_accepts_no_header_by_default():
    model = get_plugin("receiver").config_class.model_validate({})
    assert model.server.auth.accepted_headers == []
    assert _app_default("contract", "dfe-receiver", "server")["auth"]["accepted_headers"] == []


def test_the_production_receiver_dials_the_loader_the_app_manifest_places():
    # The Service name with no namespace resolves wherever the suite is installed.
    production = yaml_load_string(dict(_RECEIVER)["receiver-production.yaml"]) or {}
    assert production["loader"]["address"] == catalogue.push_address(
        catalogue.descriptor("dfe-loader")
    )


def test_no_shipped_fetcher_config_authors_a_dlq_topic():
    # dfe-fetcher embeds scalo's DlqConfig (dlq.kafka.common_topic), so a
    # dlq.topic written here was read by nothing; the deploy layer's env owns it.
    for name, text in _SHIPPED:
        if not name.startswith("fetcher-"):
            continue
        routing = (yaml_load_string(text) or {}).get("routing") or {}
        assert "dlq" not in routing, f"{name} still authors routing.dlq"
