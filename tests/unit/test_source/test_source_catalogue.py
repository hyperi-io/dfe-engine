#  Project:      dfe-engine
#  File:         tests/unit/test_source/test_source_catalogue.py
#  Purpose:      Reading a shipped source catalogue, and compiling one entry into a source
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""What a catalogue entry becomes, asserted on the write body it compiles to.

The vendored fixture is the real shipped catalogue, so these run against the
shapes a deployment actually gets rather than against a hand-built entry that
could quietly diverge from it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dfe_engine.manifest import ManifestError
from dfe_engine.settings import DFESettings
from dfe_engine.source import catalogue as sc
from dfe_engine.yaml_utils import yaml_dump

SHIPPED = Path(__file__).parents[2] / "fixtures" / "catalogue" / "sources.yaml"


@pytest.fixture(scope="module")
def shipped() -> sc.SourceCatalogue:
    """The vendored copy of the catalogue dfe-transform-elastic publishes."""
    catalogue = sc.load_source_catalogue(SHIPPED)
    assert catalogue is not None
    return catalogue


@pytest.fixture
def settings() -> DFESettings:
    return DFESettings(env="dev")


def _write(tmp_path: Path, entries: dict, name: str = "sources.yaml") -> Path:
    path = tmp_path / name
    yaml_dump({"sources": entries}, path)
    return path


class TestReading:
    def test_a_deployment_with_no_catalogue_mounted_has_none(self, tmp_path, monkeypatch):
        monkeypatch.delenv(sc.CATALOGUE_FILE_ENV, raising=False)
        monkeypatch.setattr(sc, "DEFAULT_CATALOGUE_PATH", tmp_path / "absent.yaml")

        assert sc.load_source_catalogue() is None

    def test_the_environment_names_the_mounted_file(self, tmp_path, monkeypatch):
        path = _write(
            tmp_path,
            {
                "thing": {
                    "package": "p",
                    "data_stream": "d",
                    "intakes": ["beats"],
                    "transforms": ["default"],
                }
            },
        )
        monkeypatch.setenv(sc.CATALOGUE_FILE_ENV, str(path))

        catalogue = sc.load_source_catalogue()

        assert catalogue is not None
        assert catalogue.path == path
        assert catalogue.app.service == "dfe-transform-elastic"
        assert catalogue.engine == "elastic"

    def test_a_file_no_app_ships_is_refused_by_name(self, tmp_path):
        # The owning app is matched on the filename it declares, so a file no app
        # claims has no engine to name and no variant pattern to render.
        path = _write(tmp_path, {"thing": {}}, name="somebody-elses.yaml")

        with pytest.raises(ManifestError, match=r"somebody-elses\.yaml"):
            sc.load_source_catalogue(path)

    def test_a_named_file_that_is_not_there_is_refused(self, tmp_path):
        with pytest.raises(ManifestError, match="not found"):
            sc.load_source_catalogue(tmp_path / "sources.yaml")

    def test_a_document_without_the_declared_entries_key_names_that_key(self, tmp_path):
        path = tmp_path / "sources.yaml"
        yaml_dump({"entries": {"thing": {}}}, path)

        with pytest.raises(sc.SourceCatalogueError, match="'sources' mapping"):
            sc.load_source_catalogue(path)

    def test_an_entry_missing_a_key_names_the_entry(self, tmp_path):
        path = _write(tmp_path, {"thing": {"package": "p", "intakes": ["beats"]}})

        with pytest.raises(sc.SourceCatalogueError, match="'thing' is missing"):
            sc.load_source_catalogue(path)

    def test_an_intake_no_source_definition_maps_to_is_refused(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "thing": {
                    "package": "p",
                    "data_stream": "d",
                    "intakes": ["beats", "carrier-pigeon"],
                    "transforms": ["default"],
                }
            },
        )

        with pytest.raises(sc.SourceCatalogueError, match="carrier-pigeon"):
            sc.load_source_catalogue(path)

    def test_the_shipped_catalogue_parses_whole(self, shipped):
        assert len(shipped.entries) == 1069
        assert shipped.entries["okta"].dataset == "okta.system"
        assert shipped.entries["okta"].beats == {"module": "okta", "fileset": "system"}
        assert shipped.entries["fortinet"].framing == "line"


class TestSourceName:
    def test_the_separator_is_swapped_for_the_one_a_label_allows(self, shipped):
        assert shipped.entries["aws_cloudtrail"].source_name() == "aws-cloudtrail"

    def test_a_name_too_long_for_a_label_is_refused_rather_than_truncated(self, shipped):
        entry = shipped.entries["microsoft_defender_endpoint_machine_action"]

        with pytest.raises(ValueError, match="exceeds max length"):
            entry.source_name()

    def test_a_name_starting_with_a_digit_is_refused(self, shipped):
        with pytest.raises(ValueError, match="DNS-1123"):
            shipped.entries["1password_audit_events"].source_name()


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

    def test_a_package_no_fetcher_polls_is_refused_by_package_name(self, shipped, settings):
        entry = shipped.entries["zoom_activity"]

        with pytest.raises(sc.SourceCatalogueError, match="'zoom' package"):
            sc.write_request_for(shipped, entry, intake="fetcher", settings=settings)

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

    def test_the_shipped_meta_schema_is_pinned_when_one_exists(self, shipped, settings):
        write = sc.write_request_for(
            shipped, shipped.entries["aws_cloudtrail"], intake="fetcher", settings=settings
        )

        assert write.schema_config is not None
        assert write.schema_config.meta_schema == "meta/aws/cloudtrail"

    def test_no_meta_schema_is_pinned_when_none_is_shipped(self, shipped, settings):
        write = sc.write_request_for(
            shipped, shipped.entries["fortinet"], intake="receiver", settings=settings
        )

        assert write.schema_config is None

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
