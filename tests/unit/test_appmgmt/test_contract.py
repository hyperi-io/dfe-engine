#  Project:      dfe-engine
#  File:         tests/unit/test_appmgmt/test_contract.py
#  Purpose:      Tests for the container-contract reader and its config resolver
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The reader and resolver, against the contracts the six pinned images emitted.

The fixtures are the real files, byte for byte, so a flattener that stops
descending or starts double counting shows up as a changed leaf total rather than
as a plausible-looking list nobody checks.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dfe_engine.appmgmt import contract

FIXTURES = Path(__file__).parents[2] / "fixtures" / "contract"

# What each app's contract flattens to. Asserted exactly: these move only when an
# app changes its own config or the flattener changes what it counts as an option.
LEAF_COUNTS = {
    "dfe-receiver": 310,
    "dfe-loader": 175,
    "dfe-archiver": 76,
    "dfe-fetcher": 314,
    "dfe-transform-vrl": 45,
    "dfe-transform-vector": 70,
}

APPS = sorted(LEAF_COUNTS)

# The placeholder dfe-loader's schema carries as the default of every marked
# secret. An operator shown this in a box types around it.
REDACTED = "***REDACTED***"


@pytest.fixture(autouse=True)
def _unheld():
    """Nothing held between tests: the reader caches per service."""
    contract.reload_contracts()
    yield
    contract.reload_contracts()


def _contract(service: str) -> contract.AppContract:
    return contract.load_contract(service, FIXTURES)


class TestReadingTheMount:
    def test_a_deployment_that_mounts_nothing_says_so(self, tmp_path):
        found = contract.load_contract("dfe-loader", tmp_path)
        assert found.available is False
        assert found.source == contract.ContractSource.ABSENT
        assert found.schema == {}
        assert found.pinned_ref is None

    def test_an_app_with_no_directory_says_so(self):
        found = contract.load_contract("dfe-nonesuch", FIXTURES)
        assert found.available is False
        assert found.source == contract.ContractSource.ABSENT

    def test_a_directory_with_no_schema_says_so(self, tmp_path):
        (tmp_path / "dfe-loader").mkdir()
        found = contract.load_contract("dfe-loader", tmp_path)
        assert found.available is False

    @pytest.mark.parametrize("service", APPS)
    def test_every_emitted_contract_loads(self, service):
        found = _contract(service)
        assert found.available is True
        assert found.source == contract.ContractSource.MOUNT
        assert found.schema_version == "https://json-schema.org/draft/2020-12/schema"
        assert found.schema["title"] == "Config"

    def test_the_emit_step_reports_which_image_wrote_it(self, tmp_path):
        app = tmp_path / "dfe-loader"
        app.mkdir()
        (app / contract.SCHEMA_FILE).write_text(json.dumps({"type": "object"}))
        (app / contract.SOURCE_FILE).write_text(
            json.dumps({"ref": "ghcr.io/x/dfe-loader@sha256:a"})
        )
        assert contract.load_contract("dfe-loader", tmp_path).pinned_ref == (
            "ghcr.io/x/dfe-loader@sha256:a"
        )

    def test_no_source_file_is_not_a_failure(self):
        # dfe-infra writes it in the emit wrapper; a mount made another way has none.
        assert _contract("dfe-loader").pinned_ref is None

    def test_a_schema_that_is_not_json_is_reported_rather_than_ignored(self, tmp_path):
        app = tmp_path / "dfe-loader"
        app.mkdir()
        (app / contract.SCHEMA_FILE).write_text("not json")
        with pytest.raises(contract.ContractError, match="not readable"):
            contract.load_contract("dfe-loader", tmp_path)

    def test_the_env_var_names_the_directory(self, monkeypatch):
        monkeypatch.setenv(contract.CONTRACT_DIR_ENV, str(FIXTURES))
        assert contract.contract("dfe-loader").available is True

    def test_the_conventional_mount_is_the_fallback(self, monkeypatch):
        monkeypatch.delenv(contract.CONTRACT_DIR_ENV, raising=False)
        assert contract.contract_root() == contract.DEFAULT_CONTRACT_DIR


class TestFlattening:
    @pytest.mark.parametrize("service", APPS)
    def test_the_leaf_count_is_what_the_app_declares(self, service):
        view = contract.resolve_config(_contract(service), {})
        assert len(view.fields) == LEAF_COUNTS[service]

    @pytest.mark.parametrize("service", APPS)
    def test_every_option_is_addressable_as_an_overlay_path(self, service):
        view = contract.resolve_config(_contract(service), {})
        assert all(f.path.startswith("config.") for f in view.fields)
        assert len({f.path for f in view.fields}) == len(view.fields)

    def test_a_section_is_not_itself_an_option(self):
        paths = {f.path for f in contract.resolve_config(_contract("dfe-loader"), {}).fields}
        assert "config.clickhouse" not in paths
        assert "config.clickhouse.protocol" in paths

    def test_an_optional_block_expands_to_its_own_options(self):
        # `tls` is anyOf[TlsConfig, null]; one box holding an object is no use to
        # the console, and nothing else would ever show skip_verify.
        paths = {f.path for f in contract.resolve_config(_contract("dfe-loader"), {}).fields}
        assert "config.clickhouse.tls.skip_verify" in paths

    def test_an_array_is_one_option_rather_than_its_elements(self):
        by_path = {f.path: f for f in contract.resolve_config(_contract("dfe-loader"), {}).fields}
        assert by_path["config.clickhouse.hosts"].type == "array"
        assert by_path["config.clickhouse.hosts"].default == ["localhost:8123"]

    def test_a_leaf_default_beats_the_parent_object_default(self):
        by_path = {f.path: f for f in contract.resolve_config(_contract("dfe-loader"), {}).fields}
        # ClickHouseConfig sets protocol, and the $ref site repeats it in a whole
        # object default. They agree here; the rule has to pick one regardless.
        assert by_path["config.clickhouse.protocol"].default == "http"
        assert by_path["config.batch_processing.max_chunk_size"].default == 10000

    def test_a_rust_enum_is_offered_as_a_closed_set(self):
        # The apps spell an enum as a oneOf of consts, not as `enum`.
        by_path = {f.path: f for f in contract.resolve_config(_contract("dfe-loader"), {}).fields}
        assert by_path["config.clickhouse.insert_format"].enum == ["rowbinary", "jsoneachrow"]

    def test_the_capability_catalogue_supplies_an_enum_the_schema_omits(self):
        by_path = {f.path: f for f in contract.resolve_config(_contract("dfe-loader"), {}).fields}
        # The schema calls protocol a plain string; only the catalogue says native
        # is refused at startup.
        assert by_path["config.clickhouse.protocol"].enum == ["native", "http"]


class TestSecrets:
    @pytest.mark.parametrize("service", APPS)
    def test_a_secret_never_carries_a_value_or_a_default(self, service):
        view = contract.resolve_config(_contract(service), {})
        secrets = [f for f in view.fields if f.secret]
        assert secrets, service
        assert all(f.value is None and f.default is None for f in secrets)

    @pytest.mark.parametrize("service", APPS)
    def test_the_redaction_placeholder_never_reaches_a_response(self, service):
        view = contract.resolve_config(_contract(service), {})
        assert REDACTED not in json.dumps([[f.default, f.value] for f in view.fields])

    def test_an_unmarked_password_is_still_a_secret(self):
        # dfe-receiver ships no x-dfe-secret at all, so the marker alone would hand
        # an operator's Kafka password back over the API.
        by_path = {f.path: f for f in contract.resolve_config(_contract("dfe-receiver"), {}).fields}
        assert by_path["config.kafka.sasl.password"].secret is True

    def test_a_marked_secret_is_a_secret_whatever_it_is_called(self):
        by_path = {f.path: f for f in contract.resolve_config(_contract("dfe-loader"), {}).fields}
        assert by_path["config.clickhouse.password"].secret is True

    def test_a_written_secret_reports_that_it_is_set_without_saying_what(self):
        overlay = {"config": {"clickhouse": {"password": "hunter2"}}}
        by_path = {
            f.path: f for f in contract.resolve_config(_contract("dfe-loader"), overlay).fields
        }
        written = by_path["config.clickhouse.password"]
        assert written.is_set is True
        assert written.value is None
        assert written.provenance == contract.Provenance.OVERLAY


class TestProvenance:
    def test_an_untouched_option_carries_the_app_default(self):
        by_path = {f.path: f for f in contract.resolve_config(_contract("dfe-loader"), {}).fields}
        field = by_path["config.batch_processing.max_chunk_size"]
        assert field.provenance == contract.Provenance.DEFAULT
        assert field.value == 10000
        assert field.is_set is False

    def test_a_written_option_reports_the_override_and_keeps_the_default(self):
        overlay = {"config": {"batch_processing": {"max_chunk_size": 5000}}}
        by_path = {
            f.path: f for f in contract.resolve_config(_contract("dfe-loader"), overlay).fields
        }
        field = by_path["config.batch_processing.max_chunk_size"]
        assert field.provenance == contract.Provenance.OVERLAY
        assert (field.value, field.default, field.is_set) == (5000, 10000, True)

    def test_an_option_with_no_default_is_unset_rather_than_empty(self):
        view = contract.resolve_config(_contract("dfe-archiver"), {})
        unset = [f for f in view.fields if f.provenance == contract.Provenance.UNSET]
        assert unset
        assert all(f.value is None and f.default is None for f in unset)

    def test_the_one_key_the_loader_chart_bakes_is_chart_derived(self):
        by_path = {f.path: f for f in contract.resolve_config(_contract("dfe-loader"), {}).fields}
        assert by_path["config.clickhouse.protocol"].provenance == contract.Provenance.CHART

    def test_the_chart_wins_over_an_overlay_key_it_overrides(self):
        # The deployment decides it, so reporting the overlay would tell the
        # console an operator governs a value the chart replaces.
        overlay = {"config": {"clickhouse": {"protocol": "native"}}}
        by_path = {
            f.path: f for f in contract.resolve_config(_contract("dfe-loader"), overlay).fields
        }
        assert by_path["config.clickhouse.protocol"].provenance == contract.Provenance.CHART

    @pytest.mark.parametrize("service", ["dfe-transform-vrl", "dfe-transform-vector"])
    def test_the_transform_families_the_chart_derives(self, service):
        view = contract.resolve_config(_contract(service), {})
        derived = {f.path for f in view.fields if f.provenance == contract.Provenance.CHART}
        assert {
            "config.source.transport",
            "config.sink.transport",
            "config.source.listen",
            "config.sink.endpoint",
            "config.source.brokers",
            "config.sink.brokers",
            "config.source.topics",
            "config.sink.topic",
            "config.source.group_id",
            "config.source.sasl.username",
            "config.source.sasl.password",
            "config.sink.sasl.username",
            "config.sink.sasl.password",
        } == derived

    def test_an_app_the_chart_derives_nothing_for_has_no_chart_fields(self):
        view = contract.resolve_config(_contract("dfe-receiver"), {})
        assert not [f for f in view.fields if f.provenance == contract.Provenance.CHART]


class TestUnknownKeys:
    def test_an_overlay_key_the_contract_does_not_declare_comes_back(self):
        overlay = {"config": {"retired_section": {"key": "value"}}}
        view = contract.resolve_config(_contract("dfe-loader"), overlay)
        assert [(u.path, u.value) for u in view.unknown] == [
            ("config.retired_section.key", "value")
        ]

    def test_a_declared_key_is_not_unknown(self):
        overlay = {"config": {"batch_processing": {"max_chunk_size": 5000}}}
        assert contract.resolve_config(_contract("dfe-loader"), overlay).unknown == []

    def test_writing_a_declared_array_is_not_unknown(self):
        # flatten indexes list items, so hosts[0] has to resolve back to hosts.
        overlay = {"config": {"clickhouse": {"hosts": ["ch-0:8123", "ch-1:8123"]}}}
        assert contract.resolve_config(_contract("dfe-loader"), overlay).unknown == []

    def test_chart_values_outside_the_config_block_are_not_config(self):
        # replicaCount and image.tag are the scaling and values routes' business.
        overlay = {"replicaCount": 3, "image": {"tag": "v1.2.3"}}
        assert contract.resolve_config(_contract("dfe-loader"), overlay).unknown == []

    def test_with_no_contract_every_overlay_key_is_unknown(self, tmp_path):
        overlay = {"config": {"clickhouse": {"database": "dfe"}}}
        view = contract.resolve_config(contract.load_contract("dfe-loader", tmp_path), overlay)
        assert view.available is False
        assert view.fields == []
        assert [u.path for u in view.unknown] == ["config.clickhouse.database"]


class TestProtectedPaths:
    def test_the_policy_decides_which_paths_are_protected(self):
        view = contract.resolve_config(
            _contract("dfe-loader"),
            {},
            is_protected=lambda path: path == "config.clickhouse.hosts",
        )
        protected = [f.path for f in view.fields if f.protected]
        assert protected == ["config.clickhouse.hosts"]
