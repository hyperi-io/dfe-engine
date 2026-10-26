#  Project:      dfe-engine
#  File:         tests/unit/test_source/source_catalogue_cases.py
#  Purpose:      Case tables for the source catalogue unit tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Case tables for the source catalogue unit tests."""

import copy
from pathlib import Path
from typing import Any, TypedDict

import yaml

from dfe_engine.source.catalogue import Availability, CatalogueIntake
from dfe_engine.source.models import SourceTransform
from tests.unit.test_source.factories import (
    CATALOGUE_FIXTURES,
    make_source_transform,
    make_unvalidated_source_transform,
)

ELASTIC_CONTRACT = CATALOGUE_FIXTURES / "contract"
NO_CONTRACT = CATALOGUE_FIXTURES / "absent"

CISCO_ASA_BODY: dict[str, Any] = {
    "source": "cisco-asa",
    "display_name": "cisco_asa.log",
    "description": "The log data stream of the Elastic cisco_asa integration.",
    "match": {"field": "_source", "operator": "equals", "value": "cisco-asa"},
    "schema": {
        "meta_schema": "meta/elastic/ecs",
        "derived_schema": "derived/cisco_asa/log",
        "additional_fields": "additional/cisco_asa/log",
    },
    "transform": {"engine": "elastic", "variant": "filebeat.cisco_asa.default"},
    "transport": None,
    "archive": False,
}

CISCO_ASA_BEATS_BODY: dict[str, Any] = {
    **CISCO_ASA_BODY,
    "match": {"field": "data_stream.dataset", "operator": "equals", "value": "cisco_asa.log"},
}

COREDNS_LOG_BODY: dict[str, Any] = {
    "source": "coredns-log",
    "display_name": "coredns.log",
    "description": "The log data stream of the Elastic coredns integration.",
    "match": {"field": "data_stream.dataset", "operator": "equals", "value": "coredns.log"},
    "schema": {
        "meta_schema": "meta/elastic/ecs",
        "derived_schema": "derived/coredns/log",
        "additional_fields": "additional/coredns/log",
    },
    "transform": {"engine": "elastic", "variant": "filebeat.coredns_log.default"},
    "transport": None,
    "archive": False,
}

WELL_FORMED_FILE: dict[str, Any] = {
    "source": "acme-log",
    "match": {"field": "data_stream.dataset", "operator": "equals", "value": "acme.log"},
    "transform": {"engine": "elastic", "variant": "filebeat.acme_log.default"},
    "catalogue": {
        "package": "acme",
        "data_stream": "log",
        "intakes": ["beats"],
        "variants": ["filebeat.acme_log.default"],
    },
}
"""A catalogue file every skip row breaks one rule of, written as ``acme-log.yaml``."""


def _document(*, changes: dict[str, Any]) -> str:
    """The well-formed file with each dotted key set to its value, or removed where it is None."""
    result = copy.deepcopy(WELL_FORMED_FILE)
    for dotted, value in changes.items():
        *parents, leaf = dotted.split(".")
        target = result
        for key in parents:
            target = target[key]
        if value is None:
            target.pop(leaf, None)
        else:
            target[leaf] = value
    return yaml.safe_dump(result, sort_keys=False)


class DefaultIntakeCase(TypedDict):
    id: str
    entry: str
    expected_default_intake: CatalogueIntake


DEFAULT_INTAKE_CASES: list[DefaultIntakeCase] = [
    {"id": "fetcher_first", "entry": "aws-cloudtrail", "expected_default_intake": "fetcher"},
    {"id": "beats_before_receiver", "entry": "cisco-asa", "expected_default_intake": "beats"},
    {"id": "beats_alone", "entry": "coredns-log", "expected_default_intake": "beats"},
]


class LoadSourceCatalogueCase(TypedDict):
    id: str
    entry: str
    expected_block: dict[str, Any]


LOAD_SOURCE_CATALOGUE_CASES: list[LoadSourceCatalogueCase] = [
    {
        "id": "receiver_default_with_framing",
        "entry": "cisco-asa",
        "expected_block": {
            "beats": {},
            "data_stream": "log",
            "framing": "line",
            "intakes": ("beats", "receiver"),
            "package": "cisco_asa",
            "variants": ("filebeat.cisco_asa.default",),
        },
    },
    {
        "id": "fetcher_default_with_a_beats_module",
        "entry": "okta",
        "expected_block": {
            "beats": {"module": "okta", "fileset": "system"},
            "data_stream": "system",
            "framing": None,
            "intakes": ("beats", "fetcher"),
            "package": "okta",
            "variants": ("filebeat.okta.default",),
        },
    },
    {
        "id": "two_variants_default_first",
        "entry": "coredns-log",
        "expected_block": {
            "beats": {},
            "data_stream": "log",
            "framing": None,
            "intakes": ("beats",),
            "package": "coredns",
            "variants": ("filebeat.coredns_log.default", "filebeat.coredns_log.json"),
        },
    },
    {
        "id": "renamed_source_keeps_the_original_variant",
        "entry": "onepassword-audit-events",
        "expected_block": {
            "beats": {},
            "data_stream": "audit_events",
            "framing": None,
            "intakes": ("beats", "fetcher"),
            "package": "1password",
            "variants": ("filebeat.1password_audit_events.default",),
        },
    },
]


class LoadSourceCatalogueSkipCase(TypedDict):
    id: str
    text: str
    expected_reason: str


LOAD_SOURCE_CATALOGUE_SKIP_CASES: list[LoadSourceCatalogueSkipCase] = [
    {
        "id": "not_readable_yaml",
        "text": "source: [",
        "expected_reason": "it is not readable YAML: while parsing a flow node",
    },
    {"id": "not_a_mapping", "text": "- acme-log\n", "expected_reason": "it is not a mapping"},
    {
        "id": "a_key_a_catalogue_file_does_not_take",
        "text": _document(changes={"enabled": False}),
        "expected_reason": "it carries key(s) a catalogue file does not take: enabled",
    },
    {
        "id": "no_catalogue_block",
        "text": _document(changes={"catalogue": None}),
        "expected_reason": "it carries no 'catalogue' mapping",
    },
    {
        "id": "a_catalogue_block_that_is_not_a_mapping",
        "text": _document(changes={"catalogue": ["acme"]}),
        "expected_reason": "it carries no 'catalogue' mapping",
    },
    {
        "id": "not_a_write_body",
        "text": _document(changes={"fetcher": {"source_type": "aws", "topic": "own"}}),
        "expected_reason": "it is not a source write body: Value error, a source is "
        "receiver-based (match or when) or fetcher-based (fetcher): set exactly one",
    },
    {
        "id": "an_engine_the_app_manifest_lacks",
        "text": _document(changes={"transform.engine": "nope"}),
        "expected_reason": "it is not a source write body: transform.engine: Value error, "
        "Invalid transform engine 'nope'.",
    },
    {
        "id": "a_source_type_the_app_manifest_lacks",
        "text": _document(
            changes={
                "catalogue.intakes": ["beats", "fetcher"],
                "fetcher": {"source_type": "nope", "topic": "own"},
                "match": None,
            }
        ),
        "expected_reason": "it is not a source write body: fetcher.source_type: Value error, "
        "Unknown fetcher source_type 'nope'.",
    },
    {
        "id": "source_is_not_the_file_name",
        "text": _document(changes={"source": "acme-other"}),
        "expected_reason": "it creates source 'acme-other', not its file name",
    },
    {
        "id": "no_intakes",
        "text": _document(changes={"catalogue.intakes": []}),
        "expected_reason": "its catalogue block is not valid: intakes: List should have at "
        "least 1 item after validation, not 0",
    },
    {
        "id": "intakes_that_are_not_a_list",
        "text": _document(changes={"catalogue.intakes": "beats"}),
        "expected_reason": "its catalogue block is not valid: intakes: Input should be a valid "
        "list",
    },
    {
        "id": "an_unknown_intake",
        "text": _document(changes={"catalogue.intakes": ["beats", "email"]}),
        "expected_reason": "its catalogue block is not valid: intakes.1: Input should be "
        "'beats', 'receiver' or 'fetcher'",
    },
    {
        "id": "a_package_that_is_not_a_string",
        "text": _document(changes={"catalogue.package": 7}),
        "expected_reason": "its catalogue block is not valid: package: Input should be a valid "
        "string",
    },
    {
        "id": "no_package",
        "text": _document(changes={"catalogue.package": None}),
        "expected_reason": "its catalogue block is not valid: package: Field required",
    },
    {
        "id": "beats_that_are_not_a_mapping",
        "text": _document(changes={"catalogue.beats": "acme"}),
        "expected_reason": "its catalogue block is not valid: beats: Input should be a valid "
        "dictionary",
    },
    {
        "id": "a_fetcher_intake_without_a_fetcher_body",
        "text": _document(changes={"catalogue.intakes": ["beats", "fetcher"]}),
        "expected_reason": "it must carry fetcher exactly when fetcher is an intake",
    },
    {
        "id": "no_variants",
        "text": _document(changes={"catalogue.variants": []}),
        "expected_reason": "its catalogue block lists no variants for its transform",
    },
]


class VariantAvailabilityCase(TypedDict):
    id: str
    contract_root: Path
    transform: SourceTransform | None
    expected_state: Availability
    expected_reason: str


VARIANT_AVAILABILITY_CASES: list[VariantAvailabilityCase] = [
    {
        "id": "no_transform",
        "contract_root": ELASTIC_CONTRACT,
        "transform": None,
        "expected_state": "available",
        "expected_reason": "the source selects no compiled variant",
    },
    {
        "id": "listed",
        "contract_root": ELASTIC_CONTRACT,
        "transform": make_source_transform(variant="filebeat.cisco_asa.default"),
        "expected_state": "available",
        "expected_reason": "the deployed 'dfe-transform-elastic' ships 'filebeat.cisco_asa.default'",
    },
    {
        "id": "not_listed",
        "contract_root": ELASTIC_CONTRACT,
        "transform": make_source_transform(variant="filebeat.o365_metrics_entra_alerts.default"),
        "expected_state": "missing_variant",
        "expected_reason": "the deployed 'dfe-transform-elastic' does not ship transform variant "
        "'filebeat.o365_metrics_entra_alerts.default'",
    },
    {
        "id": "no_contract_mounted",
        "contract_root": NO_CONTRACT,
        "transform": make_source_transform(variant="filebeat.cisco_asa.default"),
        "expected_state": "unknown",
        "expected_reason": "no 'dfe-transform-elastic' contract is mounted, so its variants are "
        "not known",
    },
    {
        "id": "a_transform_the_app_manifest_does_not_declare",
        "contract_root": ELASTIC_CONTRACT,
        "transform": make_unvalidated_source_transform(engine="nope", variant="filebeat.x.default"),
        "expected_state": "unknown",
        "expected_reason": "the app manifest declares no 'dfe-transform-nope', so its variants "
        "are not known",
    },
]


class WriteRequestForCase(TypedDict):
    id: str
    entry: str
    arguments: dict[str, Any]
    expected_body: dict[str, Any]


WRITE_REQUEST_FOR_CASES: list[WriteRequestForCase] = [
    {
        "id": "default_on_a_beats_and_receiver_entry_matches_the_dataset",
        "entry": "cisco-asa",
        "arguments": {},
        "expected_body": CISCO_ASA_BEATS_BODY,
    },
    {
        "id": "explicit_receiver_matches_the_source_label",
        "entry": "cisco-asa",
        "arguments": {"intake": "receiver"},
        "expected_body": CISCO_ASA_BODY,
    },
    {
        "id": "explicit_beats_matches_the_dataset",
        "entry": "cisco-asa",
        "arguments": {"intake": "beats"},
        "expected_body": CISCO_ASA_BEATS_BODY,
    },
    {
        "id": "receiver_renamed_matches_the_new_name",
        "entry": "cisco-asa",
        "arguments": {"intake": "receiver", "name": "edge-firewall"},
        "expected_body": {
            **CISCO_ASA_BODY,
            "source": "edge-firewall",
            "match": {"field": "_source", "operator": "equals", "value": "edge-firewall"},
        },
    },
    {
        "id": "beats_renamed_still_matches_the_dataset",
        "entry": "cisco-asa",
        "arguments": {"name": "edge-firewall"},
        "expected_body": {**CISCO_ASA_BEATS_BODY, "source": "edge-firewall"},
    },
    {
        "id": "fetcher_default_carries_no_match",
        "entry": "aws-cloudtrail",
        "arguments": {},
        "expected_body": {
            "source": "aws-cloudtrail",
            "display_name": "aws.cloudtrail",
            "description": "The cloudtrail data stream of the Elastic aws integration.",
            "schema": {
                "meta_schema": "meta/elastic/ecs",
                "derived_schema": "derived/aws/cloudtrail",
                "additional_fields": "additional/aws/cloudtrail",
            },
            "transform": {"engine": "elastic", "variant": "filebeat.aws_cloudtrail.default"},
            "fetcher": {"source_type": "aws", "topic": "own"},
            "transport": None,
            "archive": False,
        },
    },
    {
        "id": "beats_on_a_fetcher_entry_drops_the_fetcher",
        "entry": "okta",
        "arguments": {"intake": "beats"},
        "expected_body": {
            "source": "okta",
            "display_name": "okta.system",
            "description": "The system data stream of the Elastic okta integration.",
            "match": {"field": "data_stream.dataset", "operator": "equals", "value": "okta.system"},
            "schema": {
                "meta_schema": "meta/elastic/ecs",
                "derived_schema": "derived/okta/system",
                "additional_fields": "additional/okta/system",
            },
            "transform": {"engine": "elastic", "variant": "filebeat.okta.default"},
            "transport": None,
            "archive": False,
        },
    },
    {
        "id": "a_later_variant",
        "entry": "coredns-log",
        "arguments": {"variant": "filebeat.coredns_log.json"},
        "expected_body": {
            **COREDNS_LOG_BODY,
            "transform": {"engine": "elastic", "variant": "filebeat.coredns_log.json"},
        },
    },
    {
        "id": "the_callers_transport_and_archive",
        "entry": "coredns-log",
        "arguments": {"archive": True, "transport": "bus"},
        "expected_body": {**COREDNS_LOG_BODY, "transport": "bus", "archive": True},
    },
]


class WriteRequestForRejectCase(TypedDict):
    id: str
    entry: str
    arguments: dict[str, Any]
    message: str


WRITE_REQUEST_FOR_REJECT_CASES: list[WriteRequestForRejectCase] = [
    {
        "id": "an_intake_the_entry_does_not_arrive_by",
        "entry": "okta",
        "arguments": {"intake": "receiver"},
        "message": "catalogue entry 'okta' does not arrive by 'receiver'; it arrives by beats, "
        "fetcher",
    },
    {
        "id": "a_variant_the_entry_does_not_compile_to",
        "entry": "cisco-asa",
        "arguments": {"variant": "filebeat.cisco_asa.json"},
        "message": "catalogue entry 'cisco-asa' has no variant 'filebeat.cisco_asa.json'; it has "
        "filebeat.cisco_asa.default",
    },
    {
        "id": "a_name_that_is_not_a_label",
        "entry": "cisco-asa",
        "arguments": {"name": "Edge_Firewall"},
        "message": "Source name 'Edge_Firewall' must be a Kubernetes DNS-1123 label starting with "
        "a letter (use '-' instead of '_'): a source-bound app such as a transform or a fetcher "
        "is deployed as one instance per source, named for the source, so the name becomes an "
        "Argo Application and a set of Kubernetes object names",
    },
]
