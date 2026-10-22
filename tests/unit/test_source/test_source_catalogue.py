#  Project:      dfe-engine
#  File:         tests/unit/test_source/test_source_catalogue.py
#  Purpose:      Reading a shipped source catalogue, and compiling one entry into a source
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What a catalogue entry becomes, asserted on the write body it compiles to.

The fixture is the catalogue the installed dfe-schemas ships, so these run against
the shapes a deployment actually gets rather than against a hand-built entry that
could quietly diverge from it.
"""

from pathlib import Path

import pytest

from dfe_engine.settings import DFESettings
from dfe_engine.source import catalogue as sc
from dfe_engine.yaml_utils import yaml_dump


@pytest.fixture(scope="module")
def shipped() -> sc.SourceCatalogue:
    """The catalogue the installed dfe-schemas ships."""
    return sc.load_source_catalogue()


@pytest.fixture
def settings() -> DFESettings:
    return DFESettings(env="dev")


def _entry(source: str, *, engine: str = "elastic", **catalogue: object) -> dict:
    block = {
        "package": "p",
        "data_stream": "d",
        "intakes": ["beats"],
        "variants": ["filebeat.thing.default"],
        **catalogue,
    }
    return {"source": source, "transform": {"engine": engine}, "catalogue": block}


def _write(tmp_path: Path, *docs: dict) -> Path:
    for doc in docs:
        yaml_dump(doc, tmp_path / f"{doc.get('source', 'unnamed')}.yaml")
    return tmp_path


class TestReading:
    def test_the_catalogue_is_read_from_the_installed_dfe_schemas(self, shipped):
        assert shipped.path == sc.catalogue_dir()
        assert shipped.app.service == "dfe-transform-elastic"
        assert shipped.engine == "elastic"

    def test_a_directory_with_no_catalogue_files_is_refused(self, tmp_path):
        with pytest.raises(sc.SourceCatalogueError, match="no source catalogue"):
            sc.load_source_catalogue(tmp_path)

    def test_an_engine_no_app_catalogues_is_refused_by_name(self, tmp_path):
        _write(tmp_path, _entry("thing", engine="nobody"))

        with pytest.raises(sc.SourceCatalogueError, match="dfe-transform-nobody"):
            sc.load_source_catalogue(tmp_path)

    def test_an_entry_missing_a_key_names_the_file(self, tmp_path):
        doc = _entry("thing")
        del doc["catalogue"]["data_stream"]
        _write(tmp_path, doc)

        with pytest.raises(sc.SourceCatalogueError, match=r"thing\.yaml is missing"):
            sc.load_source_catalogue(tmp_path)

    def test_a_variant_the_pattern_cannot_produce_is_refused(self, tmp_path):
        _write(tmp_path, _entry("thing", variants=["thing.default"]))

        with pytest.raises(sc.SourceCatalogueError, match="do not follow"):
            sc.load_source_catalogue(tmp_path)

    def test_an_entry_without_a_default_transform_is_refused(self, tmp_path):
        _write(tmp_path, _entry("thing", variants=["filebeat.thing.audit"]))

        with pytest.raises(sc.SourceCatalogueError, match="'default'"):
            sc.load_source_catalogue(tmp_path)

    def test_an_intake_no_source_definition_maps_to_is_refused(self, tmp_path):
        _write(tmp_path, _entry("thing", intakes=["beats", "carrier-pigeon"]))

        with pytest.raises(sc.SourceCatalogueError, match="carrier-pigeon"):
            sc.load_source_catalogue(tmp_path)

    def test_a_source_name_no_label_admits_is_refused(self, tmp_path):
        _write(tmp_path, _entry("1thing"))

        with pytest.raises(sc.SourceCatalogueError, match="DNS-1123"):
            sc.load_source_catalogue(tmp_path)

    def test_the_shipped_catalogue_parses_whole(self, shipped):
        assert len(shipped.entries) == 1069
        assert shipped.entries["okta"].dataset == "okta.system"
        assert shipped.entries["okta"].beats == {"module": "okta", "fileset": "system"}
        assert shipped.entries["fortinet"].framing == "line"


class TestSourceName:
    def test_the_catalogue_names_each_source(self, shipped):
        assert shipped.entries["aws_cloudtrail"].source_name() == "aws-cloudtrail"

    def test_a_vendor_name_too_long_for_a_label_ships_a_shorter_one(self, shipped):
        entry = shipped.entries["microsoft_defender_endpoint_machine_action"]

        assert entry.source_name() == "ms-defender-endpoint-machine-action"

    def test_a_vendor_name_starting_with_a_digit_ships_one_that_does_not(self, shipped):
        assert shipped.entries["1password_audit_events"].source_name() == "onepassword-audit-events"


class TestCompile:
    def test_a_beats_intake_is_recognised_by_the_dataset_it_stamps(self, shipped, settings):
        write = sc.write_request_for(
            shipped, shipped.entries["okta"], intake="beats", settings=settings
        )

        assert write.source == "okta"
        assert write.fetcher is None
        assert write.match is not None
        assert write.match.field == "data_stream.dataset"
        assert write.match.operator == "equals"
        assert write.match.value == "okta.system"
        assert write.transform is not None
        assert write.transform.engine == "elastic"
        assert write.transform.variant == "filebeat.okta.default"

    def test_a_pushed_intake_is_recognised_by_the_label_its_sender_stamps(self, shipped, settings):
        write = sc.write_request_for(
            shipped, shipped.entries["fortinet"], intake="receiver", settings=settings
        )

        assert write.match is not None
        assert write.match.field == "_source"
        assert write.match.value == "fortinet"
        assert write.transform is not None
        assert write.transform.variant == "filebeat.fortinet.default"

    def test_a_pulled_intake_becomes_a_fetcher_stanza_and_no_rule(self, shipped, settings):
        write = sc.write_request_for(
            shipped, shipped.entries["aws_cloudtrail"], intake="fetcher", settings=settings
        )

        assert write.match is None
        assert write.fetcher is not None
        assert write.fetcher.source_type == "aws"
        assert write.fetcher.topic == "own"

    def test_a_family_the_catalogue_spells_differently_still_maps(self, shipped, settings):
        write = sc.write_request_for(
            shipped,
            shipped.entries["1password_audit_events"],
            intake="fetcher",
            settings=settings,
            name="onepassword-audit",
        )

        assert write.fetcher is not None
        assert write.fetcher.source_type == "onepassword"

    def test_the_shipped_catalogue_offers_fetcher_only_where_a_family_polls(self, shipped):
        offered = [e for e in shipped.entries.values() if "fetcher" in e.intakes]

        assert offered
        assert "fetcher" not in shipped.entries["zoom_activity"].intakes
        for entry in offered:
            assert sc.apps.source_type_for_package(entry.package), entry.name

    def test_a_package_no_fetcher_polls_is_refused_by_package_name(
        self, shipped, settings, tmp_path
    ):
        _write(
            tmp_path,
            _entry(
                "zoom-activity",
                package="zoom",
                data_stream="activity",
                intakes=["fetcher"],
                variants=["filebeat.zoom_activity.default"],
            ),
        )
        catalogue = sc.load_source_catalogue(tmp_path)

        with pytest.raises(sc.SourceCatalogueError, match="'zoom' package"):
            sc.write_request_for(
                catalogue, catalogue.entries["zoom_activity"], intake="fetcher", settings=settings
            )

    def test_an_intake_the_entry_does_not_arrive_by_is_refused(self, shipped, settings):
        with pytest.raises(sc.SourceCatalogueError, match="does not arrive by 'receiver'"):
            sc.write_request_for(
                shipped, shipped.entries["okta"], intake="receiver", settings=settings
            )

    def test_a_transform_the_entry_does_not_ship_is_refused(self, shipped, settings):
        with pytest.raises(sc.SourceCatalogueError, match="no 'device' transform"):
            sc.write_request_for(
                shipped,
                shipped.entries["okta"],
                intake="beats",
                settings=settings,
                transform="device",
            )

    def test_a_supplied_name_replaces_the_derived_one_everywhere(self, shipped, settings):
        write = sc.write_request_for(
            shipped,
            shipped.entries["fortinet"],
            intake="receiver",
            settings=settings,
            name="edge-firewall",
        )

        assert write.source == "edge-firewall"
        assert write.match is not None
        assert write.match.value == "edge-firewall"

    def test_the_transport_and_archive_are_the_callers(self, shipped, settings):
        write = sc.write_request_for(
            shipped,
            shipped.entries["okta"],
            intake="beats",
            settings=settings,
            transport="bus",
            archive=True,
        )

        assert write.transport == "bus"
        assert write.archive is True
