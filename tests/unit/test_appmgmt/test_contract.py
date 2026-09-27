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

import json
from pathlib import Path

import pytest

from dfe_engine.appmgmt import contract
from dfe_engine.gitcrud.engine import set_path

FIXTURES = Path(__file__).parents[2] / "fixtures" / "contract"

# Schemas from the builds that hold each source acknowledgement until delivery,
# copied byte for byte from each app's docs/config-schema.json: dfe-receiver
# f61c903, dfe-loader edef368, dfe-transform-vrl da42bd2, dfe-archiver 29f201a,
# dfe-transform-vector 36dbfa6, dfe-fetcher 0ab0317.
HELD = Path(__file__).parents[2] / "fixtures" / "contract-acknowledgements"

# Every block of each app that holds its acknowledgement, as its schema declares it.
HOLDING_BLOCKS = [
    ("dfe-receiver", "server"),
    ("dfe-receiver", "grpc"),
    ("dfe-receiver", "otlp"),
    ("dfe-receiver", "splunk_hec"),
    ("dfe-receiver", "lumberjack"),
    ("dfe-receiver", "fluent"),
    ("dfe-receiver", "webhook"),
    ("dfe-receiver", "prometheus_rw"),
    ("dfe-loader", "kafka"),
    ("dfe-loader", "grpc"),
    ("dfe-transform-vrl", "source"),
    ("dfe-archiver", "kafka"),
    ("dfe-archiver", "grpc"),
    ("dfe-transform-vector", "source"),
    ("dfe-fetcher", "extractors.vector"),
]

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


def _marked_root(root: Path) -> Path:
    """A mounted dfe-receiver contract whose one marked field has a name that says nothing."""
    schema = {
        "type": "object",
        "properties": {
            "server": {
                "type": "object",
                "properties": {"banner": {"type": "string", contract.SECRET_MARKER: True}},
            }
        },
    }
    (root / "dfe-receiver").mkdir()
    (root / "dfe-receiver" / contract.SCHEMA_FILE).write_text(json.dumps(schema))
    return root


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

    def test_a_list_of_tokens_is_a_secret_before_the_app_marks_it(self):
        # The shipped receiver marks nothing, and its bearer tokens are a list.
        by_path = {f.path: f for f in contract.resolve_config(_contract("dfe-receiver"), {}).fields}
        assert by_path["config.server.auth.bearer.tokens"].secret is True

    @pytest.mark.parametrize(
        "path",
        [
            "config.server.auth.bearer.tokens",
            "config.server.auth.header_values",
            "config.otlp.auth.header_values",
        ],
    )
    def test_a_marker_on_a_list_s_items_hides_the_list(self, path):
        found = contract.load_contract("dfe-receiver", HELD)
        by_path = {f.path: f for f in contract.resolve_config(found, {}).fields}
        assert by_path[path].secret is True

    def test_a_marked_field_of_a_listed_object_is_masked_and_the_rest_shown(self):
        # Each accepted header carries a name and its marked values; the name is a
        # setting an operator needs, the values are the credential.
        found = contract.load_contract("dfe-receiver", HELD)
        overlay = {
            "config": {
                "server": {"auth": {"accepted_headers": [{"name": "x-key", "values": ["s3"]}]}}
            }
        }
        by_path = {f.path: f for f in contract.resolve_config(found, overlay).fields}
        headers = by_path["config.server.auth.accepted_headers"]
        assert (headers.secret, headers.is_set) == (False, True)
        assert headers.value == [{"name": "x-key", "values": [contract.REDACTED]}]

    def test_the_shipped_fetcher_s_connection_tokens_stay_hidden(self):
        # dfe-fetcher marks each connection's token inside the list, so the list
        # itself carried no marker and came back whole.
        overlay = {
            "config": {"sources": {"okta": {"connections": [{"token": "s3", "domain": "d"}]}}}
        }
        view = contract.resolve_config(_contract("dfe-fetcher"), overlay)
        connections = {f.path: f for f in view.fields}["config.sources.okta.connections"]
        assert connections.value == [{"token": contract.REDACTED, "domain": "d"}]
        assert "s3" not in json.dumps([[f.value, f.default] for f in view.fields])

    @pytest.mark.parametrize(
        ("path", "entry", "shown", "credential"),
        [
            (
                "config.sources.rest",
                {"url": "https://api.example", "topic": "t", "auth": {"token": "t-1"}},
                {"url": "https://api.example", "topic": "t", "auth": {"token": contract.REDACTED}},
                "t-1",
            ),
            (
                "config.sources.db",
                {"dialect": "postgres", "connection_string": "postgres://u:p@h/db"},
                {"dialect": "postgres", "connection_string": contract.REDACTED},
                "postgres://u:p@h/db",
            ),
        ],
    )
    def test_a_map_entry_shows_with_only_its_credential_fields_masked(
        self, path, entry, shown, credential
    ):
        # dfe-fetcher keys its REST and DB sources by name, and marks the credential
        # fields of each entry.
        found = contract.load_contract("dfe-fetcher", HELD)
        overlay: dict = {}
        set_path(overlay, path, {"primary": entry})
        field = {f.path: f for f in contract.resolve_config(found, overlay).fields}[path]
        assert field.secret is False
        assert field.value == {"primary": shown}
        assert credential not in json.dumps(field.value)

    def test_a_masked_map_entry_written_back_restores(self):
        found = contract.load_contract("dfe-fetcher", HELD)
        path = "config.sources.rest"
        stored: dict = {}
        set_path(stored, path, {"primary": {"url": "u", "auth": {"token": "t-1"}}})
        field = {f.path: f for f in contract.resolve_config(found, stored).fields}[path]
        edited = {"primary": {**field.value["primary"], "url": "u-2"}}
        assert contract.restore_masked_at(stored, path, edited) == {
            "primary": {"url": "u-2", "auth": {"token": "t-1"}}
        }

    @pytest.mark.parametrize(("marked", "secret"), [(False, False), (True, True)])
    def test_a_definition_that_lists_itself_is_judged_and_ends(self, marked, secret):
        node: dict = {"type": "string"}
        if marked:
            node[contract.SECRET_MARKER] = True
        schema = {
            "type": "object",
            "properties": {"tree": {"type": "array", "items": {"$ref": "#/$defs/Node"}}},
            "$defs": {
                "Node": {
                    "type": "object",
                    "properties": {
                        "label": node,
                        "children": {"type": "array", "items": {"$ref": "#/$defs/Node"}},
                    },
                }
            },
        }
        found = contract.AppContract(
            service="dfe-loader",
            available=True,
            source=contract.ContractSource.MOUNT,
            schema=schema,
        )
        overlay = {"config": {"tree": [{"label": "l-1", "children": [{"label": "l-2"}]}]}}
        (tree,) = contract.resolve_config(found, overlay).fields
        # The tree itself is not a credential; only a marked label inside it is.
        assert tree.secret is False
        shown = contract.REDACTED if secret else None
        assert tree.value == [{"label": shown or "l-1", "children": [{"label": shown or "l-2"}]}]

    def test_a_non_secret_list_is_still_shown(self):
        by_path = {f.path: f for f in contract.resolve_config(_contract("dfe-loader"), {}).fields}
        assert by_path["config.clickhouse.hosts"].secret is False

    def test_an_undeclared_key_named_like_a_credential_is_masked(self):
        overlay = {"config": {"retired": {"password": "hunter2", "host": "h"}}}
        view = contract.resolve_config(_contract("dfe-loader"), overlay)
        assert {(u.path, u.value) for u in view.unknown} == {
            ("config.retired.password", contract.REDACTED),
            ("config.retired.host", "h"),
        }

    def test_a_written_secret_reports_that_it_is_set_without_saying_what(self):
        # A secret the chart does not supply: the loader's warehouse and Kafka
        # credentials arrive by env, so the overlay never governs those.
        overlay = {"config": {"geoip": {"auto_download": {"ipinfo_token": "hunter2"}}}}
        by_path = {
            f.path: f for f in contract.resolve_config(_contract("dfe-loader"), overlay).fields
        }
        written = by_path["config.geoip.auto_download.ipinfo_token"]
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

    def test_the_flat_env_families_the_loader_chart_sets(self):
        # Every var the loader Deployment sets, mapped to the path it outranks.
        view = contract.resolve_config(_contract("dfe-loader"), {})
        derived = {f.path for f in view.fields if f.provenance == contract.Provenance.CHART}
        assert {
            "config.transport",
            "config.grpc.listen",
            "config.kafka.brokers",
            "config.kafka.sasl.username",
            "config.kafka.sasl.password",
            "config.clickhouse.hosts",
            "config.clickhouse.database",
            "config.clickhouse.username",
            "config.clickhouse.password",
            "config.clickhouse.protocol",
            "config.routing.dlq.topic",
            "config.routing.dlq.mode",
        } == derived

    def test_the_flat_env_families_the_receiver_chart_sets(self):
        view = contract.resolve_config(_contract("dfe-receiver"), {})
        derived = {f.path for f in view.fields if f.provenance == contract.Provenance.CHART}
        assert {
            "config.kafka.brokers",
            "config.kafka.sasl.username",
            "config.kafka.sasl.password",
            "config.kafka.sasl.mechanism",
            "config.server.bind_address",
            "config.routing.dlq.enabled",
            "config.routing.dlq.topic",
            "config.routing.dlq.mode",
        } == derived

    def test_the_flat_env_families_the_fetcher_chart_sets(self):
        view = contract.resolve_config(_contract("dfe-fetcher"), {})
        derived = {f.path for f in view.fields if f.provenance == contract.Provenance.CHART}
        assert {
            "config.kafka.sasl.username",
            "config.kafka.sasl.password",
            "config.kafka.sasl.mechanism",
            "config.dlq.enabled",
            "config.dlq.mode",
            "config.dlq.kafka.common_topic",
            "config.dlq.kafka.routing",
        } == derived

    def test_the_flat_env_families_the_archiver_chart_sets(self):
        # The archiver reads its own bare KAFKA_*/ARCHIVER_*/S3_*/DLQ_* names,
        # not the DFE_ARCHIVER_* family the other apps use.
        view = contract.resolve_config(_contract("dfe-archiver"), {})
        derived = {f.path for f in view.fields if f.provenance == contract.Provenance.CHART}
        assert {
            "config.transport",
            "config.grpc.listen",
            "config.kafka.brokers",
            "config.kafka.security_protocol",
            "config.kafka.sasl_username",
            "config.kafka.sasl_password",
            "config.kafka.sasl_mechanism",
            "config.kafka.topic_include",
            "config.archive.destination",
            "config.archive.s3.endpoint",
            "config.archive.s3.bucket",
            "config.dlq.enabled",
            "config.dlq.mode",
            "config.dlq.kafka.common_topic",
            "config.dlq.kafka.routing",
        } == derived

    def test_what_supplies_a_chart_path_is_named(self):
        # A refused write has to say what to change instead of the overlay.
        assert contract.chart_supplier("dfe-receiver", "config.server.bind_address") == (
            "DFE_RECEIVER_BIND_ADDRESS"
        )
        assert contract.chart_supplier("dfe-loader", "config.batch_processing.format") is None

    def test_the_env_names_a_chart_sets_are_available_as_data(self):
        # What an extraEnv key is compared against, so the name is a key rather
        # than something to read out of the supplier string.
        names = contract.chart_env_names("dfe-loader")
        assert names["DFE_LOADER_KAFKA_BROKERS"] == "config.kafka.brokers"
        assert "DFE_LOADER_HOUSE_KEY" not in names

    def test_a_chart_helper_is_not_an_env_name(self):
        # The transforms resolve four paths through dfe-common.transport, which
        # names no variable an operator could shadow.
        names = contract.chart_env_names("dfe-transform-vrl")
        assert "dfe-common.transport" not in names
        assert names["DFE_TRANSFORM_SOURCE_BROKERS"] == "config.source.brokers"

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

    def test_a_path_outside_the_table_is_the_overlay_s(self):
        # The table is data: a path nothing names in it is an operator's to write.
        overlay = {"config": {"batch_processing": {"max_chunk_size": 5000}}}
        by_path = {
            f.path: f for f in contract.resolve_config(_contract("dfe-loader"), overlay).fields
        }
        written = by_path["config.batch_processing.max_chunk_size"]
        assert written.provenance == contract.Provenance.OVERLAY


class TestUnknownKeys:
    def test_an_overlay_key_the_contract_does_not_declare_comes_back(self):
        overlay = {"config": {"retired_section": {"setting": "value"}}}
        view = contract.resolve_config(_contract("dfe-loader"), overlay)
        assert [(u.path, u.value) for u in view.unknown] == [
            ("config.retired_section.setting", "value")
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


class TestCustomEnvKeys:
    def test_an_extra_env_key_is_reported_apart_from_the_unknown_block(self):
        overlay = {"extraEnv": {"DFE_LOADER_HOUSE_STYLE": "kept"}}
        view = contract.resolve_config(_contract("dfe-loader"), overlay)
        assert [(c.path, c.value) for c in view.custom] == [
            ("extraEnv.DFE_LOADER_HOUSE_STYLE", "kept")
        ]
        assert view.unknown == []

    def test_a_custom_key_named_like_a_credential_reports_no_value(self):
        overlay = {"extraEnv": {"DFE_LOADER_S3_SECRET": "s3", "DFE_LOADER_HOUSE_STYLE": "kept"}}
        view = contract.resolve_config(_contract("dfe-loader"), overlay)
        assert [(c.path, c.value) for c in view.custom] == [
            ("extraEnv.DFE_LOADER_HOUSE_STYLE", "kept"),
            ("extraEnv.DFE_LOADER_S3_SECRET", contract.REDACTED),
        ]

    @pytest.mark.parametrize(
        "name",
        [
            "DFE_LOADER_KAFKA_SASL_PASSWORD",
            "GITHUB_TOKEN",
            "OAUTH_CLIENT_SECRET",
            "S3_API_KEY",
            "S3_SECRET_KEY",
            "AWS_SECRET_ACCESS_KEY",
            "MAXMIND_LICENSE_KEY",
            "SIGNING_PRIVATE_KEY",
            "TOKEN",
            "RECEIVER_BEARER_TOKENS",
        ],
    )
    def test_an_environment_name_ending_in_a_credential_word_is_secret(self, name):
        assert contract.secret_env_name(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "DFE_LOADER_HOUSE_STYLE",
            "TOKEN_URL",
            "SECRET_SOURCE",
            "KEYCLOAK_REALM",
            "MONKEY",
            "DFE_LOADER_HOUSE_KEY",
            "KAFKA_PARTITION_KEY",
            "KEY",
        ],
    )
    def test_a_name_that_only_contains_one_is_not(self, name):
        assert contract.secret_env_name(name) is False

    @pytest.mark.parametrize(
        ("name", "section"),
        [
            ("api_key", ""),
            ("private_key", ""),
            ("secret_access_key", "s3"),
            ("service_account_key", "gcp"),
            ("maxmind_license_key", "auto_download"),
            ("key", "tls"),
            ("key", "client_auth"),
        ],
    )
    def test_a_qualified_key_is_a_secret(self, name, section):
        assert contract.secret_name(name, section) is True

    @pytest.mark.parametrize(
        ("name", "section"),
        [
            ("key", "routing"),
            ("key", ""),
            ("partition_key", "sink"),
            ("routing_key", ""),
            ("integration_key", "duo"),
            ("key_field", "sink"),
        ],
    )
    def test_a_key_nothing_qualifies_is_not(self, name, section):
        assert contract.secret_name(name, section) is False

    @pytest.mark.parametrize(
        ("name", "section"),
        [
            ("auth_token", "ingest"),
            ("sasl_password", "kafka"),
            ("aws_session_token", "sasl"),
            ("client_secret", "azure"),
            ("oauth_client_secret", "sasl"),
            ("bearer_tokens", "auth"),
        ],
    )
    def test_a_config_key_ending_in_a_credential_word_is_secret(self, name, section):
        # dfe-fetcher's ingest.auth_token carries no marker, so its name is all there is.
        assert contract.secret_name(name, section) is True

    @pytest.mark.parametrize(
        ("name", "section"),
        [
            ("credential_secret", "github"),
            ("private_key_secret", "salesforce"),
            ("key_secret", "tls"),
            ("ca_secret", "tls"),
            ("cert_secret", "tls"),
            ("config_secret", ""),
            ("secret_source", "bearer"),
            ("token_url_override", "gcp"),
        ],
    )
    def test_a_secret_source_or_a_name_that_only_contains_one_is_not(self, name, section):
        # A secret source holds a provider:path:key spec, which an operator needs to see.
        assert contract.secret_name(name, section) is False

    def test_an_app_with_no_custom_env_reports_none(self):
        assert contract.resolve_config(_contract("dfe-loader"), {}).custom == []

    @pytest.mark.parametrize("name", ["DFE_X", "A", "DFE_LOADER_HOUSE_KEY", "X9_Y"])
    def test_an_environment_name_is_accepted(self, name):
        assert contract.ENV_NAME.match(name)

    @pytest.mark.parametrize("name", ["lower", "9LEADING", "_LEADING", "HAS-DASH", "HAS.DOT", ""])
    def test_anything_else_is_not_an_environment_name(self, name):
        assert not contract.ENV_NAME.match(name)

    def test_an_environment_value_is_one_scalar(self):
        assert contract.check_env_value("plain") == ""
        assert contract.check_env_value(7) == ""
        assert "not a list or a mapping" in contract.check_env_value(["a"])
        assert "not a list or a mapping" in contract.check_env_value({"a": 1})

    def test_a_line_break_would_be_a_second_key(self):
        assert "second key" in contract.check_env_value("one\nTWO=smuggled")


class TestJudgingAWrite:
    def _option(self, service: str, path: str) -> contract.DeclaredOption:
        return contract.declared_options(_contract(service))[path]

    def test_every_declared_option_is_judgeable(self):
        options = contract.declared_options(_contract("dfe-loader"))
        assert len(options) == LEAF_COUNTS["dfe-loader"]
        assert all(path.startswith("config.") for path in options)

    def test_a_value_of_the_declared_type_is_accepted(self):
        option = self._option("dfe-loader", "config.batch_processing.max_chunk_size")
        assert contract.check_value(option, 5000) == ""

    def test_a_value_of_the_wrong_type_says_which_type_it_takes(self):
        option = self._option("dfe-loader", "config.batch_processing.max_chunk_size")
        reason = contract.check_value(option, "lots")
        assert "takes integer, not string" in reason

    def test_a_bool_is_not_an_integer(self):
        # Python's bool is an int, so an unguarded check writes `true` as 1.
        option = self._option("dfe-loader", "config.batch_processing.max_chunk_size")
        assert "not boolean" in contract.check_value(option, True)

    def test_a_rust_enum_refuses_a_branch_it_does_not_declare(self):
        # The apps spell an enum as a oneOf of consts, which is the only closed
        # set most of them carry.
        option = self._option("dfe-loader", "config.clickhouse.insert_format")
        assert contract.check_value(option, "rowbinary") == ""
        assert "takes one of" in contract.check_value(option, "csv")

    def test_the_catalogue_s_enum_refuses_what_the_schema_would_accept(self):
        # The schema calls protocol a plain string; only the capability catalogue
        # says which two values the app actually takes.
        option = self._option("dfe-loader", "config.clickhouse.protocol")
        assert contract.check_value(option, "http") == ""
        assert "takes one of" in contract.check_value(option, "native-ish")

    def test_a_nullable_option_takes_a_null_and_a_plain_one_does_not(self):
        nullable = self._option("dfe-loader", "config.routing.mapping_file")
        assert nullable.nullable is True
        assert contract.check_value(nullable, None) == ""
        plain = self._option("dfe-loader", "config.batch_processing.max_chunk_size")
        assert "does not accept a null" in contract.check_value(plain, None)

    def test_an_array_takes_a_list(self):
        option = self._option("dfe-loader", "config.clickhouse.hosts")
        assert contract.check_value(option, ["ch-0:8123"]) == ""
        assert "takes array" in contract.check_value(option, "ch-0:8123")


class TestProtectedPaths:
    def test_the_policy_decides_which_paths_are_protected(self):
        view = contract.resolve_config(
            _contract("dfe-loader"),
            {},
            is_protected=lambda path: path == "config.clickhouse.hosts",
        )
        protected = [f.path for f in view.fields if f.protected]
        assert protected == ["config.clickhouse.hosts"]


class TestHeldAcknowledgements:
    """The overlay route judges a write against the mounted schema, so it takes the key as is."""

    @pytest.mark.parametrize(("service", "block"), HOLDING_BLOCKS)
    def test_the_key_is_a_declared_boolean(self, service, block):
        options = contract.declared_options(contract.load_contract(service, HELD))
        option = options[f"config.{block}.acknowledgements.enabled"]
        assert option.type == "boolean"

    @pytest.mark.parametrize(("service", "block"), HOLDING_BLOCKS)
    def test_turning_it_off_is_accepted_and_anything_else_is_not(self, service, block):
        options = contract.declared_options(contract.load_contract(service, HELD))
        option = options[f"config.{block}.acknowledgements.enabled"]
        assert contract.check_value(option, False) == ""
        assert "takes boolean" in contract.check_value(option, "off")

    @pytest.mark.parametrize(("service", "block"), HOLDING_BLOCKS)
    def test_an_untouched_block_holds_by_default(self, service, block):
        found = contract.load_contract(service, HELD)
        by_path = {f.path: f for f in contract.resolve_config(found, {}).fields}
        held = by_path[f"config.{block}.acknowledgements.enabled"]
        assert (held.value, held.provenance) == (True, contract.Provenance.DEFAULT)

    @pytest.mark.parametrize(("service", "block"), HOLDING_BLOCKS)
    def test_a_written_false_is_the_overlay_s(self, service, block):
        found = contract.load_contract(service, HELD)
        overlay: dict = {}
        set_path(overlay, f"config.{block}.acknowledgements.enabled", False)
        view = contract.resolve_config(found, overlay)
        held = {f.path: f for f in view.fields}[f"config.{block}.acknowledgements.enabled"]
        assert (held.value, held.provenance) == (False, contract.Provenance.OVERLAY)
        assert view.unknown == []

    @pytest.mark.parametrize(("service", "block"), HOLDING_BLOCKS)
    def test_no_chart_supplies_it(self, service, block):
        # A chart-derived path is refused on write; this one is the operator's.
        path = f"config.{block}.acknowledgements.enabled"
        assert contract.chart_supplier(service, path) is None


class TestRedactingTheOverlay:
    """What the values route hands back: the overlay as written, less its credentials."""

    OVERLAY = {
        "deploy": {"service": "dfe-receiver", "instance": "default"},
        "config": {
            "server": {
                "request_timeout_ms": 30000,
                "auth": {
                    "mode": "bearer",
                    "bearer": {"tokens": ["tok-1", "tok-2"], "secret_source": None},
                    "accepted_headers": [{"name": "x-api-key", "values": ["hv-1"]}],
                    "header_values": ["legacy-1"],
                },
            },
            "kafka": {"sasl": {"username": "dfe", "password": "kafka-pw"}},
            "retired": {"api_key": "old-key"},
        },
        "extraEnv": {
            "DFE_RECEIVER_HOUSE_STYLE": "kept",
            "DFE_RECEIVER_KAFKA_SASL_PASSWORD": "env-pw",
            "S3_API_KEY": "env-key",
        },
    }

    def _redacted(self, root: Path = HELD) -> dict:
        return contract.redact_overlay(
            contract.load_contract("dfe-receiver", root), json.loads(json.dumps(self.OVERLAY))
        )

    def test_bearer_tokens_are_masked_one_for_one(self):
        auth = self._redacted()["config"]["server"]["auth"]
        assert auth["bearer"]["tokens"] == [contract.REDACTED, contract.REDACTED]

    def test_a_header_keeps_its_name_and_loses_its_values(self):
        auth = self._redacted()["config"]["server"]["auth"]
        assert auth["accepted_headers"] == [{"name": "x-api-key", "values": [contract.REDACTED]}]
        assert auth["header_values"] == [contract.REDACTED]

    def test_an_unmarked_password_is_masked_by_its_name(self):
        assert self._redacted()["config"]["kafka"]["sasl"] == {
            "username": "dfe",
            "password": contract.REDACTED,
        }

    def test_an_undeclared_key_named_like_a_credential_is_masked(self):
        assert self._redacted()["config"]["retired"] == {"api_key": contract.REDACTED}

    def test_an_environment_credential_is_masked_by_its_name(self):
        assert self._redacted()["extraEnv"] == {
            "DFE_RECEIVER_HOUSE_STYLE": "kept",
            "DFE_RECEIVER_KAFKA_SASL_PASSWORD": contract.REDACTED,
            "S3_API_KEY": contract.REDACTED,
        }

    def test_everything_else_comes_back_as_written(self):
        redacted = self._redacted()
        assert redacted["deploy"] == self.OVERLAY["deploy"]
        assert redacted["config"]["server"]["request_timeout_ms"] == 30000
        assert redacted["config"]["server"]["auth"]["bearer"]["secret_source"] is None

    def test_no_credential_survives_anywhere_in_it(self):
        text = json.dumps(self._redacted())
        for credential in (
            "tok-1",
            "tok-2",
            "hv-1",
            "legacy-1",
            "kafka-pw",
            "old-key",
            "env-pw",
            "env-key",
        ):
            assert credential not in text

    def test_a_document_naming_its_app_is_read_against_that_app(self, monkeypatch, tmp_path):
        monkeypatch.setenv(contract.CONTRACT_DIR_ENV, str(_marked_root(tmp_path)))
        contract.reload_contracts()
        doc = {"deploy": {"service": "dfe-receiver"}, "config": {"server": {"banner": "b-1"}}}
        # Only the app's marker says the banner is secret.
        assert contract.redact_resource(doc)["config"]["server"]["banner"] == contract.REDACTED

    def test_a_var_about_to_be_written_is_judged_as_its_read_would_be(self, monkeypatch, tmp_path):
        # Only the app's marker says the banner is secret, so the app the stored
        # document names has to reach the judgement.
        monkeypatch.setenv(contract.CONTRACT_DIR_ENV, str(_marked_root(tmp_path)))
        contract.reload_contracts()
        doc = {"deploy": {"service": "dfe-receiver"}}
        path = "config.server.banner"
        assert contract.redact_var(doc, path, "b-1") == contract.REDACTED
        assert contract.redact_var({}, path, "b-1") == "b-1"
        assert contract.redact_var(doc, "config.server.request_timeout_ms", 30000) == 30000

    def test_a_document_naming_no_app_is_judged_by_name(self, monkeypatch, tmp_path):
        monkeypatch.setenv(contract.CONTRACT_DIR_ENV, str(_marked_root(tmp_path)))
        contract.reload_contracts()
        doc = json.loads(json.dumps(self.OVERLAY))
        doc["deploy"]["service"] = "../dfe-receiver"
        doc["config"]["server"]["banner"] = "b-1"
        server = contract.redact_resource(doc)["config"]["server"]
        assert server["banner"] == "b-1"
        assert server["auth"]["bearer"]["tokens"] == [contract.REDACTED, contract.REDACTED]

    def test_with_no_contract_the_name_rule_still_holds(self, tmp_path):
        # Unmounted, the names are all there is to judge by.
        auth = self._redacted(tmp_path)["config"]["server"]["auth"]
        assert auth["bearer"]["tokens"] == [contract.REDACTED, contract.REDACTED]
        assert self._redacted(tmp_path)["config"]["kafka"]["sasl"]["password"] == (
            contract.REDACTED
        )

    def test_with_no_contract_header_values_are_masked_by_name(self, tmp_path):
        auth = self._redacted(tmp_path)["config"]["server"]["auth"]
        assert auth["accepted_headers"] == [{"name": "x-api-key", "values": [contract.REDACTED]}]
        assert auth["header_values"] == [contract.REDACTED]
        assert "hv-1" not in json.dumps(auth)
        assert "legacy-1" not in json.dumps(auth)

    @pytest.mark.parametrize("root", ["held", "unmounted"])
    def test_a_routing_key_shows_and_a_tls_key_masks(self, root, tmp_path):
        found = contract.load_contract("dfe-receiver", HELD if root == "held" else tmp_path)
        overlay = {
            "config": {
                "routing": {"key": "org_id"},
                "sink": {"partition_key": "host"},
                "tls": {"key": "-----BEGIN PRIVATE KEY-----"},
                "s3": {"secret_key": "s3-sk"},
            }
        }
        assert contract.redact_overlay(found, overlay)["config"] == {
            "routing": {"key": "org_id"},
            "sink": {"partition_key": "host"},
            "tls": {"key": contract.REDACTED},
            "s3": {"secret_key": contract.REDACTED},
        }

    def test_a_key_inside_a_listed_entry_is_judged_by_the_list(self):
        found = contract.load_contract("dfe-receiver", HELD)
        overlay = {"config": {"credentials": [{"key": "c-1"}], "shards": [{"key": "s-1"}]}}
        assert contract.redact_overlay(found, overlay)["config"] == {
            "credentials": [{"key": contract.REDACTED}],
            "shards": [{"key": "s-1"}],
        }

    def test_an_unmarked_licence_key_is_a_secret(self):
        by_path = {f.path: f for f in contract.resolve_config(_contract("dfe-loader"), {}).fields}
        assert by_path["config.geoip.auto_download.maxmind_license_key"].secret is True

    def test_nothing_written_stays_nothing(self):
        found = contract.load_contract("dfe-receiver", HELD)
        overlay = {"config": {"kafka": {"sasl": {"password": ""}}, "server": {"auth": {}}}}
        assert contract.redact_overlay(found, overlay) == overlay

    def test_the_stored_document_is_not_changed(self):
        found = contract.load_contract("dfe-receiver", HELD)
        overlay = json.loads(json.dumps(self.OVERLAY))
        contract.redact_overlay(found, overlay)
        assert overlay == self.OVERLAY


class TestRestoringMaskedValues:
    """A masked read written back unchanged puts the stored credential back."""

    R = contract.REDACTED
    STORED = {
        "config": {
            "kafka": {"sasl": {"password": "kafka-pw"}},
            "server": {
                "auth": {
                    "bearer": {"tokens": ["tok-1", "tok-2"]},
                    "accepted_headers": [{"name": "x-api-key", "values": ["hv-1"]}],
                }
            },
        },
        "extraEnv": {"DFE_X_TOKEN": "env-tok"},
    }

    def test_the_whole_redacted_overlay_restores_to_what_is_stored(self):
        found = contract.load_contract("dfe-receiver", HELD)
        masked = contract.redact_overlay(found, json.loads(json.dumps(self.STORED)))
        assert contract.restore_masked(masked, self.STORED) == self.STORED

    @pytest.mark.parametrize(
        ("path", "value", "restored"),
        [
            ("config.kafka.sasl.password", R, "kafka-pw"),
            ("config.server.auth.bearer.tokens", [R, R], ["tok-1", "tok-2"]),
            ("config.server.auth.bearer.tokens", [R, R, "tok-3"], ["tok-1", "tok-2", "tok-3"]),
            ("config.server.auth.bearer.tokens", ["tok-9"], ["tok-9"]),
            (
                "config.server.auth.accepted_headers",
                [{"name": "x-api-key", "values": [R, "hv-2"]}],
                [{"name": "x-api-key", "values": ["hv-1", "hv-2"]}],
            ),
            ("extraEnv.DFE_X_TOKEN", R, "env-tok"),
            ("config.kafka.sasl.password", "new-pw", "new-pw"),
        ],
    )
    def test_a_write_at_a_path(self, path, value, restored):
        assert contract.restore_masked_at(self.STORED, path, value) == restored

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            ("config.kafka.sasl.username", R),
            ("config.server.auth.bearer.tokens", [R, R, R]),
            ("extraEnv.DFE_Y_TOKEN", R),
            # A renamed header is a new entry, so its masked values have nothing behind them.
            ("config.server.auth.accepted_headers", [{"name": "x-renamed", "values": [R]}]),
        ],
    )
    def test_the_mask_with_nothing_behind_it_is_refused(self, path, value):
        with pytest.raises(contract.MaskedValueError, match="nothing is stored"):
            contract.restore_masked_at(self.STORED, path, value)

    def test_a_stored_null_is_nothing_to_restore(self):
        with pytest.raises(contract.MaskedValueError):
            contract.restore_masked_at({"a": {"password": None}}, "a.password", self.R)


class TestAMaskedListEntryKeepsItsOwnCredential:
    """A masked list entry gets back the credential it was read with, or the write is refused."""

    R = contract.REDACTED
    # dfe-fetcher's connections: one account per entry, named by a required id.
    CONNECTIONS = [
        {"id": "a", "org": "org-a", "token": "tok-a"},
        {"id": "b", "org": "org-b", "token": "tok-b"},
        {"id": "c", "org": "org-c", "token": "tok-c"},
    ]

    def _shown(self, *ids: str) -> list[dict]:
        by_id = {entry["id"]: entry for entry in self.CONNECTIONS}
        return [{**by_id[i], "token": self.R} for i in ids]

    def test_deleting_one_keeps_each_other_token_on_its_own_entry(self):
        restored = contract.restore_masked(self._shown("a", "c"), self.CONNECTIONS)
        assert restored == [self.CONNECTIONS[0], self.CONNECTIONS[2]]

    def test_reordering_keeps_each_token_on_its_own_entry(self):
        restored = contract.restore_masked(self._shown("c", "a", "b"), self.CONNECTIONS)
        assert restored == [self.CONNECTIONS[2], self.CONNECTIONS[0], self.CONNECTIONS[1]]

    def test_an_appended_entry_is_kept_as_written(self):
        added = {"id": "d", "org": "org-d", "token": "tok-d"}
        restored = contract.restore_masked([*self._shown("a", "b", "c"), added], self.CONNECTIONS)
        assert restored == [*self.CONNECTIONS, added]

    def test_an_entry_edited_around_its_masked_token_is_refused(self):
        edited = self._shown("b", "a")
        edited[0]["org"] = "org-b-2"
        with pytest.raises(contract.CredentialReentryError, match=r"connections\[0\].*org"):
            contract.restore_masked(edited, self.CONNECTIONS, path="connections")

    def test_a_renamed_entry_is_refused(self):
        renamed = self._shown("a", "b")
        renamed[1]["id"] = "b-2"
        with pytest.raises(contract.MaskedValueError, match=r"\[1\]\.token.*nothing is stored"):
            contract.restore_masked(renamed, self.CONNECTIONS, path="connections")

    def test_headers_are_matched_by_name(self):
        stored = [{"name": "x-one", "values": ["hv-1"]}, {"name": "x-two", "values": ["hv-2"]}]
        shown = [{"name": "x-two", "values": [self.R]}]
        assert contract.restore_masked(shown, stored) == [stored[1]]

    def test_unnamed_masks_restore_while_every_stored_one_is_there(self):
        # Two masks for two stored tokens, and a third written in full.
        assert contract.restore_masked([self.R, "tok-3", self.R], ["tok-1", "tok-2"]) == [
            "tok-1",
            "tok-3",
            "tok-2",
        ]

    @pytest.mark.parametrize(
        "written",
        [
            pytest.param([R], id="deleted"),
            pytest.param([R, "tok-3"], id="deleted-and-appended"),
        ],
    )
    def test_an_unnamed_deletion_is_refused(self, written):
        # Which token went cannot be told, and guessing could keep a revoked one.
        with pytest.raises(contract.MaskedValueError, match="which were removed cannot be told"):
            contract.restore_masked(written, ["tok-1", "tok-2"], path="tokens")

    def test_unnamed_entries_reordered_are_refused(self):
        stored = [{"url": "u-1", "token": "tok-1"}, {"url": "u-2", "token": "tok-2"}]
        shown = [{"url": "u-2", "token": self.R}, {"url": "u-1", "token": self.R}]
        with pytest.raises(contract.MaskedValueError, match=r"\[0\] is masked but no longer"):
            contract.restore_masked(shown, stored, path="endpoints")

    def test_unnamed_entries_unchanged_restore(self):
        stored = [{"url": "u-1", "token": "tok-1"}, {"url": "u-2", "token": "tok-2"}]
        shown = [{"url": "u-1", "token": self.R}, {"url": "u-2", "token": self.R}]
        assert contract.restore_masked(shown, stored) == stored

    def test_a_shared_name_is_not_an_identity(self):
        # Two stored entries under one id: nothing says which a mask came from.
        stored = [{"id": "a", "token": "tok-1"}, {"id": "a", "token": "tok-2"}]
        with pytest.raises(contract.MaskedValueError, match="which were removed cannot be told"):
            contract.restore_masked([{"id": "a", "token": self.R}], stored, path="connections")

    @pytest.mark.parametrize("guess", ["tok-1", "tok-wrong"])
    def test_a_guessed_token_is_refused_right_or_wrong(self, guess):
        # Accepting a right guess and refusing a wrong one would confirm the token.
        doc = {"config": {"server": {"auth": {"bearer": {"tokens": ["tok-1", "tok-2", "tok-3"]}}}}}
        path = "config.server.auth.bearer.tokens"
        with pytest.raises(contract.MaskedValueError, match="which were removed cannot be told"):
            contract.restore_masked_at(doc, path, [guess, self.R, self.R])

    def test_an_entry_with_no_credential_still_accounts_for_itself(self):
        doc = {"config": {"x": {"endpoints": [{"url": "u-1", "token": "tok-1"}, {"url": "u-2"}]}}}
        path = "config.x.endpoints"
        written = [{"url": "u-1", "token": self.R}, {"url": "u-2"}]
        assert contract.restore_masked_at(doc, path, written) == doc["config"]["x"]["endpoints"]


class TestAMaskedCredentialRestoresOnlyWhereItWasSet:
    """A masked credential restores only while every other field of its object is as stored.

    A writer who cannot read a token could otherwise point its entry at a host they
    run and have the engine send the real token there.
    """

    R = contract.REDACTED
    CONNECTIONS = [
        {"id": "a", "tenant_url": "https://a.example", "token": "tok-a-4430"},
        {"id": "b", "tenant_url": "https://b.example", "token": "tok-b-4431"},
    ]

    def test_a_named_entry_pointed_elsewhere_is_refused(self):
        written = [
            {"id": "a", "tenant_url": "https://evil.example", "token": self.R},
            {**self.CONNECTIONS[1], "token": self.R},
        ]
        with pytest.raises(contract.CredentialReentryError, match=r"connections\[0\].*tenant_url"):
            contract.restore_masked(written, self.CONNECTIONS, path="connections")

    def test_the_change_is_taken_with_the_credential_typed_again(self):
        written = [
            {"id": "a", "tenant_url": "https://new.example", "token": "tok-a-4432"},
            {**self.CONNECTIONS[1], "token": self.R},
        ]
        assert contract.restore_masked(written, self.CONNECTIONS) == [
            written[0],
            self.CONNECTIONS[1],
        ]

    def test_an_untouched_entry_beside_a_changed_one_still_restores(self):
        written = [
            {**self.CONNECTIONS[0], "token": self.R},
            {"id": "b", "tenant_url": "https://b2.example", "token": "tok-b-4433"},
        ]
        assert contract.restore_masked(written, self.CONNECTIONS) == [
            self.CONNECTIONS[0],
            written[1],
        ]

    def test_an_unnamed_entry_with_a_field_dropped_is_refused(self):
        stored = [{"url": "https://u-1.example", "tls_verify": True, "token": "tok-4434"}]
        written = [{"url": "https://u-1.example", "token": self.R}]
        with pytest.raises(contract.CredentialReentryError, match="tls_verify"):
            contract.restore_masked(written, stored, path="endpoints")

    def test_a_mapping_changed_around_its_masked_password_is_refused(self):
        doc = {"config": {"kafka": {"sasl": {"username": "dfe", "password": "kafka-pw-4435"}}}}
        written = {"sasl": {"username": "someone-else", "password": self.R}}
        with pytest.raises(contract.CredentialReentryError, match=r"config\.kafka\.sasl"):
            contract.restore_masked_at(doc, "config.kafka", written)

    def test_a_mapping_left_as_stored_restores(self):
        doc = {"config": {"kafka": {"sasl": {"username": "dfe", "password": "kafka-pw-4436"}}}}
        written = {"sasl": {"username": "dfe", "password": self.R}}
        assert contract.restore_masked_at(doc, "config.kafka", written) == {
            "sasl": {"username": "dfe", "password": "kafka-pw-4436"}
        }

    def test_a_second_credential_typed_again_is_not_a_change(self):
        doc = {
            "config": {
                "apps": [
                    {
                        "id": "a",
                        "url": "https://a.example",
                        "client_secret": "cs-4437",
                        "refresh_token": "rt-4438",
                    }
                ]
            }
        }
        written = [
            {
                "id": "a",
                "url": "https://a.example",
                "client_secret": self.R,
                "refresh_token": "rt-4439",
            }
        ]
        assert contract.restore_masked_at(doc, "config.apps", written) == [
            {**written[0], "client_secret": "cs-4437"}
        ]

    def test_a_url_moved_in_the_clear_beside_a_masked_token_is_refused(self):
        doc = {
            "config": {
                "hooks": [{"id": "a", "url": "https://u:pw-4440@a.example", "token": "tok-4441"}]
            }
        }
        written = [{"id": "a", "url": "https://u:pw-4442@evil.example", "token": self.R}]
        with pytest.raises(contract.CredentialReentryError, match="url"):
            contract.restore_masked_at(doc, "config.hooks", written)

    def test_a_setting_changed_inside_a_neighbour_holding_a_credential_is_refused(self):
        stored = [
            {
                "id": "a",
                "token": "tok-4446",
                "upstream": {"url": "https://a.example", "auth": {"password": "pw-4447"}},
            }
        ]
        written = [
            {
                "id": "a",
                "token": self.R,
                "upstream": {"url": "https://evil.example", "auth": {"password": self.R}},
            }
        ]
        with pytest.raises(contract.CredentialReentryError, match="upstream"):
            contract.restore_masked(written, stored, path="mirrors")

    def test_the_url_password_alone_typed_again_is_not_a_change(self):
        doc = {
            "config": {
                "hooks": [{"id": "a", "url": "https://u:pw-4443@a.example", "token": "tok-4444"}]
            }
        }
        written = [{"id": "a", "url": "https://u:pw-4445@a.example", "token": self.R}]
        assert contract.restore_masked_at(doc, "config.hooks", written) == [
            {**written[0], "token": "tok-4444"}
        ]


ABSENT = contract.AppContract(service="", available=False, source=contract.ContractSource.ABSENT)


class TestTheNameRuleReadsEverySpelling:
    """A credential is found by its words, however the chart or the operator spells them."""

    @pytest.mark.parametrize(
        ("name", "section"),
        [
            ("bearer-tokens", "auth"),
            ("secret-access-key", "aws"),
            ("client-secret", "azure"),
            ("service-account-key", "gcp"),
            ("rootPassword", "minio"),
            ("adminPassword", "grafana"),
            ("clientSecret", "oidc"),
            ("apiToken", "cloudflare"),
            ("sasl.jaas.config", "properties"),
            ("JWT_KEY", ""),
            ("admin_email", "google_workspace"),
            ("access_key_id", "s3"),
        ],
    )
    def test_the_name_is_a_credential(self, name, section):
        assert contract.secret_name(name, section) is True

    @pytest.mark.parametrize(
        ("name", "section"),
        [
            ("existingSecret", "kafka"),
            ("imagePullSecrets", ""),
            ("client_auth", "tls"),
            ("FOO_KEY", ""),
            ("bind_address", "server"),
            ("token_url_override", "gcp"),
            ("password_field", "session_login"),
            ("session_timeout_ms", "kafka"),
            ("cert_file", "tls"),
        ],
    )
    def test_a_setting_that_shares_a_word_is_not(self, name, section):
        assert contract.secret_name(name, section) is False

    @pytest.mark.parametrize(
        "name",
        ["KAFKA_SASL_JAAS_CONFIG", "PGPASSWORD", "HTTP_AUTHORIZATION", "REDISCLI_AUTH"],
    )
    def test_an_environment_credential_in_any_spelling_is_secret(self, name):
        assert contract.secret_env_name(name) is True

    def test_the_app_charts_own_keys_are_masked_with_no_contract(self):
        overlay = {
            "auth": {"existingSecret": "", "bearer-tokens": "chart-tok-4410"},
            "aws": {"secret-access-key": "aws-sk-4411"},
            "azure": {"client-id": "cid", "client-secret": "az-cs-4412"},
            "gcp": {"service-account-key": "gcp-sa-4413"},
            "minio": {"rootUser": "admin", "rootPassword": "minio-pw-4414"},
        }
        assert contract.redact_overlay(ABSENT, overlay) == {
            "auth": {"existingSecret": "", "bearer-tokens": contract.REDACTED},
            "aws": {"secret-access-key": contract.REDACTED},
            "azure": {"client-id": "cid", "client-secret": contract.REDACTED},
            "gcp": {"service-account-key": contract.REDACTED},
            "minio": {"rootUser": "admin", "rootPassword": contract.REDACTED},
        }

    def test_a_chart_credential_is_a_credential_path_and_is_shown_masked(self):
        receiver = {"deploy": {"service": "dfe-receiver"}}
        fetcher = {"deploy": {"service": "dfe-fetcher"}}
        assert contract.credential_var(receiver, "auth.bearer-tokens") is True
        assert contract.shown_var(fetcher, "azure.client-secret", "az-cs-4412") == contract.REDACTED

    def test_a_section_named_like_a_credential_is_read_field_by_field(self):
        overlay = {"auth": {"mode": "bearer", "bearer": {"tokens": ["tok-4415"], "header": "x"}}}
        assert contract.redact_overlay(ABSENT, overlay) == {
            "auth": {"mode": "bearer", "bearer": {"tokens": [contract.REDACTED], "header": "x"}}
        }

    def test_a_scalar_named_by_a_section_word_is_masked(self):
        overlay = {"extraEnv": {"REDISCLI_AUTH": "redis-pw-4416"}, "proxy": {"auth": "u:p"}}
        assert contract.redact_overlay(ABSENT, overlay) == {
            "extraEnv": {"REDISCLI_AUTH": contract.REDACTED},
            "proxy": {"auth": contract.REDACTED},
        }


class TestAUrlPasswordIsMasked:
    """A connection string keeps its host and user, and hides only its password."""

    def test_an_environment_url_loses_its_password(self):
        overlay = {
            "extraEnv": {
                "DATABASE_URL": "postgres://dfe:db-pw-4420@db:5432/dfe",
                "CLICKHOUSE_DSN": "clickhouse://dfe:ch-pw-4421@ch:9000",
                "PLAIN_URL": "https://example.com/path",
            }
        }
        assert contract.redact_overlay(ABSENT, overlay)["extraEnv"] == {
            "DATABASE_URL": f"postgres://dfe:{contract.REDACTED}@db:5432/dfe",
            "CLICKHOUSE_DSN": f"clickhouse://dfe:{contract.REDACTED}@ch:9000",
            "PLAIN_URL": "https://example.com/path",
        }

    def test_a_password_holding_an_at_sign_is_masked_whole(self):
        overlay = {"extraEnv": {"DATABASE_URL": "postgres://dfe:p@ss-4422@db:5432/dfe"}}
        assert contract.redact_overlay(ABSENT, overlay)["extraEnv"]["DATABASE_URL"] == (
            f"postgres://dfe:{contract.REDACTED}@db:5432/dfe"
        )

    def test_a_config_url_with_a_password_is_a_credential_write(self):
        assert contract.credential_var({}, "config.output.url", "https://u:pw-4423@host") is True
        assert contract.credential_var({}, "config.output.url", "https://host/path") is False

    def test_the_config_route_masks_a_url_password(self):
        overlay = {"config": {"clickhouse": {"hosts": ["https://dfe:ch-pw-4424@ch:8443"]}}}
        view = contract.resolve_config(_contract("dfe-loader"), overlay)
        hosts = {f.path: f for f in view.fields}["config.clickhouse.hosts"]
        assert hosts.value == [f"https://dfe:{contract.REDACTED}@ch:8443"]

    def test_a_url_written_back_as_read_keeps_its_password(self):
        doc = {"extraEnv": {"DATABASE_URL": "postgres://dfe:db-pw-4425@db/dfe"}}
        shown = f"postgres://dfe:{contract.REDACTED}@db/dfe"
        restored = contract.restore_masked_at(doc, "extraEnv.DATABASE_URL", shown)
        assert restored == "postgres://dfe:db-pw-4425@db/dfe"

    def test_a_url_changed_around_its_masked_password_is_refused(self):
        doc = {"extraEnv": {"DATABASE_URL": "postgres://dfe:db-pw-4426@db/dfe"}}
        moved = f"postgres://dfe:{contract.REDACTED}@elsewhere/dfe"
        with pytest.raises(contract.MaskedValueError, match="write it in full"):
            contract.restore_masked_at(doc, "extraEnv.DATABASE_URL", moved)


class TestACredentialInAnotherFormIsStillOne:
    """A digest, a JSON document or a PEM of a credential is judged as the credential."""

    @pytest.mark.parametrize(
        ("name", "section"),
        [
            ("password_hash", ""),
            ("seeded_password_hash", ""),
            ("passwordHash", "breakglass"),
            ("PASSWORD_DIGEST", ""),
            ("api_key_hash", ""),
            ("credentials_json", "auth"),
            ("ssl.key.pem", "librdkafka_options"),
            ("key_pem", "tls"),
        ],
    )
    def test_the_name_is_a_credential(self, name, section):
        assert contract.secret_name(name, section) is True

    @pytest.mark.parametrize(
        ("name", "section"),
        [
            ("config_hash", "deploy"),
            ("commit_hash", ""),
            ("SignatureDigest", ""),
            ("capture_json", "extract"),
            ("unwrap_nested_json", ""),
            ("ssl.ca.pem", "librdkafka_options"),
            ("ssl.key.location", "librdkafka_options"),
            ("credentials_path", "gcp"),
            ("hash", ""),
        ],
    )
    def test_a_setting_in_one_of_those_forms_is_not(self, name, section):
        assert contract.secret_name(name, section) is False

    def test_an_account_s_password_digests_are_masked(self):
        doc = {
            "username": "kaz",
            "password_hash": "$2b$12$abcdefghijklmnopqrstuv",
            "seeded_password_hash": "$2b$12$wxyzabcdefghijklmnopqr",
            "groups": ["dfe-admins"],
        }
        assert contract.redact_overlay(ABSENT, doc) == {
            "username": "kaz",
            "password_hash": contract.REDACTED,
            "seeded_password_hash": contract.REDACTED,
            "groups": ["dfe-admins"],
        }

    def test_a_librdkafka_pem_key_is_masked_and_its_location_shown(self):
        options = {"ssl.key.pem": "-----BEGIN PRIVATE KEY-----", "ssl.key.location": "/k.pem"}
        assert contract.redact_overlay(ABSENT, {"librdkafka_options": options}) == {
            "librdkafka_options": {"ssl.key.pem": contract.REDACTED, "ssl.key.location": "/k.pem"}
        }


class TestCredentialsNamedByWhatHoldsThem:
    """A credential word, an auth header's values, and the entries of a credentials map."""

    @pytest.mark.parametrize(
        ("name", "section"),
        [
            ("credential", "github"),
            ("CREDENTIAL", ""),
            ("header_values", "auth"),
            ("headerValue", ""),
            ("HEADER_VALUE", ""),
            ("values", "accepted_headers"),
            ("value", "header"),
            ("ssl_key", "kafka"),
        ],
    )
    def test_the_name_is_a_credential(self, name, section):
        assert contract.secret_name(name, section) is True

    @pytest.mark.parametrize(
        ("name", "section"),
        [
            ("header_name", "auth"),
            ("value", "conditions"),
            ("values", "labels"),
            ("credential_type", "github"),
            ("credential_secret", "github"),
        ],
    )
    def test_a_setting_beside_one_is_not(self, name, section):
        assert contract.secret_name(name, section) is False

    def test_a_credentials_map_masks_each_entry_and_a_list_of_placements_does_not(self):
        overlay = {
            "auth": {
                "mode": "credentials",
                "credentials": {"github": "vault:kv/gh:token", "pair_a": "pa-4430"},
            },
            "profile": {"credentials": [{"header": "X-Key", "from": "pair_a"}]},
        }
        assert contract.redact_overlay(ABSENT, overlay) == {
            "auth": {
                "mode": "credentials",
                "credentials": {"github": contract.REDACTED, "pair_a": contract.REDACTED},
            },
            "profile": {"credentials": [{"header": "X-Key", "from": "pair_a"}]},
        }

    def test_a_masked_credentials_map_written_back_restores(self):
        doc = {"auth": {"credentials": {"github": "gh-4433", "pair_a": "pa-4434"}}}
        shown = contract.redact_overlay(ABSENT, doc)["auth"]["credentials"]
        assert contract.restore_masked_at(doc, "auth.credentials", shown) == {
            "github": "gh-4433",
            "pair_a": "pa-4434",
        }

    def test_a_chart_s_secret_key_names_are_shown(self):
        overlay = {
            "kafka": {
                "existingSecret": "kafka-creds",
                "secretKeys": {"username": "kafka-username", "password": "kafka-password"},
                "password": "kafka-pw-4435",
            },
            "jwt": {"secretKeys": {"signing": "jwt-hmac-4436"}},
        }
        assert contract.redact_overlay(ABSENT, overlay) == {
            "kafka": {
                "existingSecret": "kafka-creds",
                "secretKeys": {"username": "kafka-username", "password": "kafka-password"},
                "password": contract.REDACTED,
            },
            # Nothing says these name a Secret's keys, so they are judged as secret keys.
            "jwt": {"secretKeys": contract.REDACTED},
        }
