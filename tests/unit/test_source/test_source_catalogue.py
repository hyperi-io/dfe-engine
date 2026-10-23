#  Project:      dfe-engine
#  File:         tests/unit/test_source/test_source_catalogue.py
#  Purpose:      Reading the dfe-schemas source catalogue, judging a variant, compiling an entry
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The source catalogue, over catalogue files copied from dfe-schemas and the installed package."""

import json
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import dfe_schemas
import pytest
import yaml
from scalo.logger import logger

from dfe_engine.source import catalogue as sc
from dfe_engine.source.models import source_from_write
from dfe_engine.source.registry import SourceRegistry, SourceValidationError
from dfe_engine.yaml_utils import yaml_load
from tests.unit.test_source.factories import (
    CATALOGUE_FIXTURES,
    make_source_catalogue,
    make_source_registry,
    make_source_transform,
)
from tests.unit.test_source.source_catalogue_cases import (
    DEFAULT_INTAKE_CASES,
    ELASTIC_CONTRACT,
    LOAD_SOURCE_CATALOGUE_CASES,
    LOAD_SOURCE_CATALOGUE_SKIP_CASES,
    VARIANT_AVAILABILITY_CASES,
    WRITE_REQUEST_FOR_CASES,
    WRITE_REQUEST_FOR_REJECT_CASES,
    DefaultIntakeCase,
    LoadSourceCatalogueCase,
    LoadSourceCatalogueSkipCase,
    VariantAvailabilityCase,
    WriteRequestForCase,
    WriteRequestForRejectCase,
)

ELASTIC = "dfe-transform-elastic"

INSTALLED_CATALOGUE = dfe_schemas.schemas_root() / sc.CATALOGUE_DIR

WELL_FORMED_ENTRY = CATALOGUE_FIXTURES / "entries" / "cisco-asa.yaml"


def _block(*, entry: sc.CatalogueEntry) -> dict[str, Any]:
    """The catalogue-block fields an entry was read with."""
    return {
        "beats": entry.beats,
        "data_stream": entry.data_stream,
        "framing": entry.framing,
        "intakes": entry.intakes,
        "package": entry.package,
        "variants": entry.variants,
    }


def _contract(*, capabilities: str, root: Path) -> Path:
    """A contract root holding the shipped elastic config schema beside *capabilities*."""
    directory = root / ELASTIC
    directory.mkdir(parents=True)
    shutil.copy(ELASTIC_CONTRACT / ELASTIC / "config-schema.json", directory)
    (directory / "capability-catalog.json").write_text(capabilities, encoding="utf-8")
    return root


def _elastic_capabilities() -> list[dict[str, Any]]:
    """The trimmed dfe-transform-elastic capability catalogue the fixtures carry."""
    text = (ELASTIC_CONTRACT / ELASTIC / "capability-catalog.json").read_text(encoding="utf-8")
    return json.loads(text)


def _parser_disagreements(*, directory: Path) -> list[str]:
    """Every catalogue file PyYAML and ruamel read as different data."""
    disagreements = []
    for path in sorted(directory.glob("*.yaml")):
        if yaml.safe_load(path.read_text(encoding="utf-8")) != yaml_load(source=path):
            disagreements.append(path.name)
    return disagreements


def _save_refusals(*, catalogue: sc.SourceCatalogue, registry: SourceRegistry) -> list[str]:
    """Every entry, intake and variant the create path refuses before it saves, with the reason."""
    refusals = []
    for entry in catalogue.entries.values():
        for intake in entry.intakes:
            for variant in (entry.variants) or ((None,)):
                try:
                    write = sc.write_request_for(entry=entry, intake=intake, variant=variant)
                    source = source_from_write(source_name=entry.name, write=write)
                    registry.validate_source(source=source)
                except (SourceValidationError, ValueError) as exc:
                    refusals.append(f"{entry.name} {intake} {variant}: {exc}")
    return refusals


@pytest.fixture
def catalogue_warnings() -> Iterator[list[dict[str, Any]]]:
    """Each warning the engine logs while the test runs, as its message plus structured fields."""

    def record(message: Any) -> None:
        warnings.append({"event": message.record["message"], **message.record["extra"]})

    warnings = []
    handler = logger.add(record, format="{message}", level="WARNING")
    try:
        yield warnings
    finally:
        logger.remove(handler)


@pytest.fixture(scope="module")
def fixture_catalogue() -> sc.SourceCatalogue:
    """The catalogue files copied from dfe-schemas into the test fixtures."""
    return make_source_catalogue()


@pytest.fixture(scope="module")
def installed() -> sc.SourceCatalogue:
    """The catalogue the installed dfe-schemas package ships."""
    return sc.load_source_catalogue()


@pytest.fixture
def registry(tmp_path: Path) -> SourceRegistry:
    """An empty source registry over a directory, which never writes in these tests."""
    return make_source_registry(sources_directory=tmp_path / "sources")


class TestCatalogueEntry:
    def test_dataset(self, fixture_catalogue: sc.SourceCatalogue):
        assert fixture_catalogue.entry(name="okta").dataset == "okta.system"

    @pytest.mark.parametrize(
        "case", DEFAULT_INTAKE_CASES, ids=[case["id"] for case in DEFAULT_INTAKE_CASES]
    )
    def test_default_intake(self, fixture_catalogue: sc.SourceCatalogue, case: DefaultIntakeCase):
        entry = fixture_catalogue.entry(name=case["entry"])
        assert entry.default_intake == case["expected_default_intake"]


class TestSourceCatalogue:
    def test_entry(self, fixture_catalogue: sc.SourceCatalogue):
        message = "no catalogue entry named 'nope'"
        with pytest.raises(sc.SourceCatalogueError, match=message):
            fixture_catalogue.entry(name="nope")


class TestLoadSourceCatalogue:
    @pytest.mark.parametrize(
        "case",
        LOAD_SOURCE_CATALOGUE_CASES,
        ids=[case["id"] for case in LOAD_SOURCE_CATALOGUE_CASES],
    )
    def test_matches_expected(
        self, fixture_catalogue: sc.SourceCatalogue, case: LoadSourceCatalogueCase
    ):
        entry = fixture_catalogue.entry(name=case["entry"])
        assert _block(entry=entry) == case["expected_block"]

    @pytest.mark.parametrize(
        "case",
        LOAD_SOURCE_CATALOGUE_SKIP_CASES,
        ids=[case["id"] for case in LOAD_SOURCE_CATALOGUE_SKIP_CASES],
    )
    def test_skips(
        self,
        catalogue_warnings: list[dict[str, Any]],
        tmp_path: Path,
        case: LoadSourceCatalogueSkipCase,
    ):
        shutil.copy(WELL_FORMED_ENTRY, tmp_path)
        (tmp_path / "acme-log.yaml").write_text(case["text"], encoding="utf-8")
        catalogue = sc.load_source_catalogue(directory=tmp_path)
        expected = case["expected_reason"]
        logged = [
            (warning["event"], warning["file"], warning["reason"][: len(expected)])
            for warning in catalogue_warnings
        ]
        assert (list(catalogue.entries), logged) == (
            ["cisco-asa"],
            [("Catalogue file skipped", "acme-log.yaml", expected)],
        )

    def test_missing_directory(self, tmp_path: Path):
        root = tmp_path / "catalogue"
        message = (
            f"the installed dfe-schemas {dfe_schemas.__version__!r} ships no catalogue at "
            f"{str(root)!r}"
        )
        with pytest.raises(sc.SourceCatalogueError, match=re.escape(message)):
            sc.load_source_catalogue(directory=root)

    def test_without_libyaml(self):
        # A PyYAML built without libyaml takes this branch, which no public call reaches on one built with it.
        assert sc._parse(libyaml=False, text="source: acme-log\n") == {"source": "acme-log"}

    def test_installed_package(self, installed: sc.SourceCatalogue):
        assert (installed.directory, bool(installed.entries)) == (INSTALLED_CATALOGUE, True)

    def test_pyyaml_and_ruamel_agree(self):
        assert _parser_disagreements(directory=INSTALLED_CATALOGUE) == []


class TestHeldSourceCatalogue:
    def test_held(self):
        assert sc.source_catalogue() is sc.source_catalogue()


class TestVariantAvailability:
    @pytest.mark.parametrize(
        "case", VARIANT_AVAILABILITY_CASES, ids=[case["id"] for case in VARIANT_AVAILABILITY_CASES]
    )
    def test_matches_expected(self, case: VariantAvailabilityCase):
        available = sc.variant_availability(
            contract_root=case["contract_root"], transform=case["transform"]
        )
        assert (available.state, available.reason) == (
            case["expected_state"],
            case["expected_reason"],
        )

    def test_unreadable_contract(self, tmp_path: Path):
        root = _contract(capabilities="[{", root=tmp_path)
        available = sc.variant_availability(
            contract_root=root, transform=make_source_transform(variant="filebeat.okta.default")
        )
        assert (available.state, available.reason) == (
            "unknown",
            f"the {ELASTIC!r} contract is mounted but not readable; "
            f"GET /api/v1/app-contracts/{ELASTIC} says why",
        )

    def test_contract_without_the_transform_capability(self, tmp_path: Path):
        others = [cap for cap in _elastic_capabilities() if cap["kind"] != "transform"]
        # A list element that is not a capability mapping is passed over, not read.
        capabilities = json.dumps(["not a capability", *others])
        root = _contract(capabilities=capabilities, root=tmp_path)
        available = sc.variant_availability(
            contract_root=root, transform=make_source_transform(variant="filebeat.okta.default")
        )
        assert (available.state, available.reason) == (
            "unknown",
            f"the {ELASTIC!r} contract lists no 'transform/elastic' capability, so its variants "
            "are not known",
        )

    def test_contract_listing_no_variants(self, tmp_path: Path):
        capabilities = _elastic_capabilities()
        transform = next(cap for cap in capabilities if cap["kind"] == "transform")
        del transform["children"]
        root = _contract(capabilities=json.dumps(capabilities), root=tmp_path)
        available = sc.variant_availability(
            contract_root=root, transform=make_source_transform(variant="filebeat.okta.default")
        )
        assert (available.state, available.reason) == (
            "unknown",
            f"the {ELASTIC!r} contract lists no variants under 'transform/elastic', so they are "
            "not known",
        )


class TestWriteRequestFor:
    @pytest.mark.parametrize(
        "case", WRITE_REQUEST_FOR_CASES, ids=[case["id"] for case in WRITE_REQUEST_FOR_CASES]
    )
    def test_matches_expected(
        self, fixture_catalogue: sc.SourceCatalogue, case: WriteRequestForCase
    ):
        write = sc.write_request_for(
            entry=fixture_catalogue.entry(name=case["entry"]), **case["arguments"]
        )
        body = write.model_dump(by_alias=True, exclude_unset=True, mode="json")
        assert body == case["expected_body"]

    @pytest.mark.parametrize(
        "case",
        WRITE_REQUEST_FOR_REJECT_CASES,
        ids=[case["id"] for case in WRITE_REQUEST_FOR_REJECT_CASES],
    )
    def test_rejects(self, fixture_catalogue: sc.SourceCatalogue, case: WriteRequestForRejectCase):
        entry = fixture_catalogue.entry(name=case["entry"])
        with pytest.raises(ValueError, match=re.escape(case["message"])):
            sc.write_request_for(entry=entry, **case["arguments"])

    def test_every_installed_case_passes_save_validation(
        self, installed: sc.SourceCatalogue, registry: SourceRegistry
    ):
        assert _save_refusals(catalogue=installed, registry=registry) == []
