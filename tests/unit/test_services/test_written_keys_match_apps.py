#  Project:      dfe-engine
#  File:         tests/unit/test_services/test_written_keys_match_apps.py
#  Purpose:      Every key the engine writes for an app is one the app reads
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Every config the engine seeds is one its app reads whole, at any depth.

serde drops an unknown key without a word, so a key spelt the engine's way rather
than the app's reads as set while the app runs on its own default: a CA file
written as ``ca_file`` left the loader verifying against the system trust store.

Each app generates ``docs/config-schema.json`` from its config structs, and the
deployment mounts that file as the app's contract. The fixtures under
``tests/fixtures/contract`` are those files at each app's latest release, and the
ones a pin in ``tests/support/producer_contract.py`` covers are checked against it.
"""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from dfe_engine.appmgmt import contract
from dfe_engine.services.registry import (
    ServiceConfigRegistry,
    default_configs,
    judge_config,
    service_apps,
)
from dfe_engine.settings import DFESettings, SecretsSettings
from dfe_engine.yaml_utils import deep_merge, yaml_load_string
from tests.support.config_schema import definitions, type_mismatches, unread_keys
from tests.support.producer_contract import (
    DFE_LOADER,
    DFE_RECEIVER,
    DFE_TRANSFORM_VECTOR,
    DFE_TRANSFORM_VRL,
    producer_file,
)

EMITTED = Path(__file__).parents[2] / "fixtures" / "contract"
_SCHEMA = "docs/config-schema.json"
# A backend the receiver reads, so the seeds do not hang on a developer .env.
_ENGINE = DFESettings(env="dev", secrets=SecretsSettings(provider="openbao"))


def _seeds() -> list[tuple[str, str, dict[str, Any]]]:
    """Every config a seed writes, as ``(app, file name, document)``."""
    found = []
    for name, text in default_configs(_ENGINE).items():
        parsed = ServiceConfigRegistry._parse_table_name(name.removesuffix(".yaml"))
        assert parsed is not None, f"{name} names no service"
        found.append((service_apps()[parsed[0]], name, yaml_load_string(text) or {}))
    return found


_SEEDS = _seeds()


def _schema(app: str) -> dict[str, Any]:
    schema = json.loads((EMITTED / app / contract.SCHEMA_FILE).read_text())
    # A reshaped schema must fail here, not pass every walk against nothing.
    assert schema.get("properties"), f"no top-level fields in {app}'s fixture"
    return schema


@pytest.mark.parametrize(("app", "name", "doc"), _SEEDS, ids=[n for _, n, _ in _SEEDS])
def test_every_seeded_config_is_one_its_app_reads_whole(app: str, name: str, doc: dict):
    schema = _schema(app)
    found = judge_config(
        contract.AppContract(
            service=app, available=True, source=contract.ContractSource.MOUNT, schema=schema
        ),
        doc,
    )
    assert {
        "unread": unread_keys(doc, schema, definitions(schema)),
        "types": type_mismatches(doc, schema, definitions(schema)),
        "refused": found.errors,
    } == {"unread": [], "types": [], "refused": []}


def test_every_app_with_a_config_file_but_vrl_and_elastic_is_seeded():
    seeded = {app for app, _, _ in _SEEDS}
    assert seeded == set(service_apps().values()) - {"dfe-transform-vrl", "dfe-transform-elastic"}


_ARCHIVER = [(name, doc) for app, name, doc in _SEEDS if app == "dfe-archiver"]


@pytest.mark.parametrize(("name", "doc"), _ARCHIVER, ids=[n for n, _ in _ARCHIVER])
def test_every_seeded_archiver_discovers_its_topics_and_routes_as_the_app_does(name, doc):
    """A non-empty kafka.topics turns discovery off, and no DFE topic is named by the seed.

    Sources land on ``<source>_land``, so a fixed list archives only what it names.
    The routing mode and default segment are the app's own.
    """
    defaults = contract.config_defaults(_schema("dfe-archiver"))
    runs = deep_merge(copy.deepcopy(defaults), doc)
    assert runs["kafka"]["topics"] == []
    assert runs["routing"] == defaults["routing"]
    assert (runs["routing"]["mode"], runs["routing"]["default_segment"]) == (
        "expression",
        "default",
    )


def test_no_loader_config_the_engine_writes_carries_coercion():
    """dfe-loader retired its coercion section, so the app's own handling decides."""
    loader = [name for app, name, doc in _SEEDS if app == "dfe-loader" and "coercion" in doc]
    assert loader == []


@pytest.mark.parametrize(
    "producer",
    [DFE_LOADER, DFE_RECEIVER, DFE_TRANSFORM_VRL, DFE_TRANSFORM_VECTOR],
    ids=lambda p: p.repo,
)
def test_the_fixture_is_the_schema_the_pinned_release_ships(producer):
    shipped = json.loads(producer_file(producer, _SCHEMA).text)
    assert _schema(producer.repo) == shipped
