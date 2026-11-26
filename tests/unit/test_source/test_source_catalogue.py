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
from dataclasses import replace
from pathlib import Path
from typing import Any

import dfe_schemas
import pytest
import yaml
from scalo.logger import logger
from scalo.rules import CompileOptions
from scalo.rules import RuleSet as ScaloRuleSet

from dfe_engine.services.routing_models import RULE_TARGETS, RuleSet
from dfe_engine.services.source_routing import RECEIVER_RULES_PROFILE, source_rules
from dfe_engine.settings import DFESettings
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


def _elastic_agent_event(*, entry: sc.CatalogueEntry) -> dict[str, Any]:
    """The envelope Elastic Agent ships a line of *entry* in, which carries no ``_source``."""
    return {
        "agent": {"type": "filebeat", "name": "agent-1", "version": "8.0.0"},
        "data_stream": {"dataset": entry.dataset, "namespace": "ep", "type": "logs"},
        "input": {"type": "tcp"},
        "message": "a vendor line",
    }


def _filebeat_event(*, entry: sc.CatalogueEntry) -> dict[str, Any]:
    """A plain Filebeat module's envelope for a line of *entry*, with no ``data_stream``."""
    return {
        "agent": {"type": "filebeat", "name": "agent-1", "version": "9.5.7"},
        "event": {"dataset": entry.dataset},
        "input": {"type": "log"},
        "message": "a vendor line",
    }


def _label_of(
    *, entry: sc.CatalogueEntry, event: dict[str, Any], intake: sc.CatalogueIntake | None
) -> Any:
    """The ``_source`` the receiver rules of the source created from *entry* on *intake* give *event*."""
    write = sc.write_request_for(entry=entry, intake=intake)
    source = source_from_write(source_name=entry.name, write=write)
    rule_set = RuleSet(targets=list(RULE_TARGETS), rules=source_rules(source))
    compiled = ScaloRuleSet.compile(
        rule_set.model_dump(mode="json", exclude_defaults=True),
        CompileOptions(profile=RECEIVER_RULES_PROFILE),
    )
    return compiled.evaluate(event).value("_source")


def _routes_to_label(*, entry: sc.CatalogueEntry, intake: sc.CatalogueIntake | None) -> bool:
    """Whether the source created from *entry* on *intake* labels its own Elastic Agent event."""
    label = _label_of(entry=entry, event=_elastic_agent_event(entry=entry), intake=intake)
    return label == entry.name


def _beats_and_receiver(*, catalogue: sc.SourceCatalogue) -> list[sc.CatalogueEntry]:
    """Every entry that arrives both by Beats and by the receiver."""
    return [
        entry
        for entry in catalogue.entries.values()
        if "beats" in entry.intakes and "receiver" in entry.intakes
    ]


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


class TestElasticAgentRouting:
    def test_installed_catalogue_has_entries_on_both_intakes(self, installed: sc.SourceCatalogue):
        assert _beats_and_receiver(catalogue=installed)

    def test_every_default_create_routes_its_elastic_agent_event(
        self, installed: sc.SourceCatalogue
    ):
        unrouted = [
            entry.name
            for entry in _beats_and_receiver(catalogue=installed)
            if not _routes_to_label(entry=entry, intake=None)
        ]
        assert unrouted == []

    def test_an_explicit_beats_create_routes_its_elastic_agent_event(
        self, installed: sc.SourceCatalogue
    ):
        unrouted = [
            entry.name
            for entry in _beats_and_receiver(catalogue=installed)
            if not _routes_to_label(entry=entry, intake="beats")
        ]
        assert unrouted == []

    def test_an_explicit_receiver_create_matches_the_source_label_only(
        self, fixture_catalogue: sc.SourceCatalogue
    ):
        entry = fixture_catalogue.entry(name="cisco-asa")
        assert not _routes_to_label(entry=entry, intake="receiver")


BEATS_ENVELOPES = [
    pytest.param(_elastic_agent_event, id="elastic-agent"),
    pytest.param(_filebeat_event, id="filebeat-module"),
]


class TestBeatsEnvelopes:
    """A beats source matches a Beats event whichever dataset field the shipper stamps."""

    @pytest.mark.parametrize("envelope", BEATS_ENVELOPES)
    @pytest.mark.parametrize("name", ["cisco-asa", "coredns-log", "okta"])
    def test_the_dataset_labels_the_event(
        self, fixture_catalogue: sc.SourceCatalogue, envelope, name: str
    ):
        entry = fixture_catalogue.entry(name=name)
        event = envelope(entry=entry)
        assert _label_of(entry=entry, event=event, intake="beats") == name

    @pytest.mark.parametrize(
        "event",
        [
            pytest.param({"data_stream": {"dataset": "coredns.log"}}, id="elastic-other-dataset"),
            pytest.param({"event": {"dataset": "coredns.log"}}, id="filebeat-other-dataset"),
            pytest.param({"event": {"dataset": "cisco_asa.log.debug"}}, id="filebeat-longer"),
            pytest.param({"event": {"dataset": "x.cisco_asa.log"}}, id="filebeat-prefixed"),
            pytest.param({"event": {"dataset": "Cisco_ASA.log"}}, id="filebeat-case"),
            pytest.param({"event": {"dataset": ""}}, id="filebeat-empty"),
            pytest.param({"event": {"module": "cisco_asa"}}, id="filebeat-module-only"),
            pytest.param({"message": "a vendor line"}, id="no-dataset"),
        ],
    )
    def test_another_dataset_is_left_for_main(
        self, fixture_catalogue: sc.SourceCatalogue, event: dict[str, Any]
    ):
        entry = fixture_catalogue.entry(name="cisco-asa")
        assert _label_of(entry=entry, event=event, intake="beats") is None


def _hub_event(*, src_type: str) -> dict[str, Any]:
    """A syslog event as an EdgeStream Hub forwards it, which carries no ``_source``."""
    return {
        "@edgestream": {
            "event": {"name": "estate-syslog", "src_type": src_type},
            "hub": {"hostname": "edgestream", "org_id": "local", "site_id": "local"},
        },
        "@timestamp": "2026-11-21T01:28:18+00:00",
        "facility": "user",
        "host": "10.66.0.102",
        "message": "a syslog line",
    }


class TestShipperRouting:
    """A receiver-only entry routes on the field its shipper sends."""

    def test_a_hub_event_is_labelled_with_the_source(self, fixture_catalogue: sc.SourceCatalogue):
        entry = fixture_catalogue.entry(name="edgestream-hub-syslog")
        label = _label_of(entry=entry, event=_hub_event(src_type="syslog_ng"), intake=None)
        assert label == "edgestream-hub-syslog"

    @pytest.mark.parametrize("src_type", ["vector", "", "SYSLOG_NG"])
    def test_another_source_type_is_left_for_main(
        self, fixture_catalogue: sc.SourceCatalogue, src_type: str
    ):
        entry = fixture_catalogue.entry(name="edgestream-hub-syslog")
        assert _label_of(entry=entry, event=_hub_event(src_type=src_type), intake=None) is None

    def test_an_event_with_no_hub_object_is_left_for_main(
        self, fixture_catalogue: sc.SourceCatalogue
    ):
        entry = fixture_catalogue.entry(name="edgestream-hub-syslog")
        assert _label_of(entry=entry, event={"message": "a syslog line"}, intake=None) is None


def _without_transform(entry: sc.CatalogueEntry, *intakes: sc.CatalogueIntake) -> sc.CatalogueEntry:
    """*entry* as a receiver-only source: no transform, so nothing needs an app of its own."""
    request = entry.request.model_copy(update={"transform": None, "fetcher": None})
    return replace(entry, request=request, intakes=intakes, variants=())


class TestIntakesRefusedHere:
    """A picker offers an entry only where the save would take it."""

    DIRECT = DFESettings(env="dev", transport={"default": "direct", "bus_present": False})
    BUS = DFESettings(env="dev", transport={"default": "bus"})

    def test_a_transformed_entry_is_refused_on_every_intake_on_direct(self, installed):
        entry = installed.entry(name="cisco-asa")

        refused = sc.intakes_refused_here(entry=entry, settings=self.DIRECT)

        assert sorted(refused) == sorted(entry.intakes)
        assert all("per-source app" in reason for reason in refused.values())

    def test_a_fetcher_intake_is_refused_on_direct_and_a_receiver_intake_is_not(self, installed):
        entry = _without_transform(installed.entry(name="okta"), "receiver")
        fetched = replace(
            installed.entry(name="okta"),
            request=installed.entry(name="okta").request.model_copy(update={"transform": None}),
            variants=(),
        )

        assert sc.intakes_refused_here(entry=entry, settings=self.DIRECT) == {}
        assert list(sc.intakes_refused_here(entry=fetched, settings=self.DIRECT)) == ["fetcher"]

    @pytest.mark.parametrize("name", ["cisco-asa", "okta"])
    def test_the_bus_refuses_nothing(self, installed, name):
        entry = installed.entry(name=name)

        assert sc.intakes_refused_here(entry=entry, settings=self.BUS) == {}

    def test_the_whole_catalogue_is_judged_without_raising(self, installed):
        refused = {
            name: sc.intakes_refused_here(entry=entry, settings=self.DIRECT)
            for name, entry in installed.entries.items()
        }

        assert len(refused) == len(installed.entries)
