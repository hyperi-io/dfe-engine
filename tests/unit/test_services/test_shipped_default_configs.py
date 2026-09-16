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

from __future__ import annotations

import re

import pytest

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


def test_no_shipped_fetcher_config_authors_a_dlq_topic():
    # dfe-fetcher embeds scalo's DlqConfig (dlq.kafka.common_topic), so a
    # dlq.topic written here was read by nothing; the deploy layer's env owns it.
    for name, text in _SHIPPED:
        if not name.startswith("fetcher-"):
            continue
        routing = (yaml_load_string(text) or {}).get("routing") or {}
        assert "dlq" not in routing, f"{name} still authors routing.dlq"
