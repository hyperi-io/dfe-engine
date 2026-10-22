#  Project:      dfe-engine
#  File:         tests/unit/test_api/sources_catalogue_cases.py
#  Purpose:      Case tables for the source catalogue route tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Case tables for the source catalogue route tests."""

from typing import Any, TypedDict

from dfe_engine.source.catalogue import Availability


class ListAgainstTheContractCase(TypedDict):
    id: str
    entry: str
    expected_availability: Availability


LIST_AGAINST_THE_CONTRACT_CASES: list[ListAgainstTheContractCase] = [
    {"id": "listed_variant", "entry": "cisco-asa", "expected_availability": "available"},
    {
        "id": "variant_the_contract_does_not_list",
        "entry": "o365-metrics-entra-alerts",
        "expected_availability": "missing_variant",
    },
]


class CreateFromCatalogueCase(TypedDict):
    id: str
    entry: str
    body: dict[str, Any]
    expected_stored: dict[str, Any]


CREATE_FROM_CATALOGUE_CASES: list[CreateFromCatalogueCase] = [
    {
        "id": "default_on_a_beats_and_receiver_entry_with_no_contract_mounted",
        "entry": "cisco-asa",
        "body": {},
        "expected_stored": {
            "fetcher": None,
            "match": {
                "field": "data_stream.dataset",
                "operator": "equals",
                "value": "cisco_asa.log",
            },
            "meta_schema": "meta/elastic/ecs",
            "origin": "receiver",
            "variant": "filebeat.cisco_asa.default",
        },
    },
    {
        "id": "explicit_receiver_matches_the_source_label",
        "entry": "cisco-asa",
        "body": {"intake": "receiver"},
        "expected_stored": {
            "fetcher": None,
            "match": {"field": "_source", "operator": "equals", "value": "cisco-asa"},
            "meta_schema": "meta/elastic/ecs",
            "origin": "receiver",
            "variant": "filebeat.cisco_asa.default",
        },
    },
    {
        "id": "receiver_renamed",
        "entry": "cisco-asa",
        "body": {"intake": "receiver", "name": "edge-firewall"},
        "expected_stored": {
            "fetcher": None,
            "match": {"field": "_source", "operator": "equals", "value": "edge-firewall"},
            "meta_schema": "meta/elastic/ecs",
            "origin": "receiver",
            "variant": "filebeat.cisco_asa.default",
        },
    },
    {
        "id": "fetcher_default",
        "entry": "aws-cloudtrail",
        "body": {},
        "expected_stored": {
            "fetcher": "aws",
            "match": None,
            "meta_schema": "meta/elastic/ecs",
            "origin": "fetcher",
            "variant": "filebeat.aws_cloudtrail.default",
        },
    },
    {
        "id": "beats_on_a_fetcher_entry",
        "entry": "okta",
        "body": {"intake": "beats"},
        "expected_stored": {
            "fetcher": None,
            "match": {"field": "data_stream.dataset", "operator": "equals", "value": "okta.system"},
            "meta_schema": "meta/elastic/ecs",
            "origin": "receiver",
            "variant": "filebeat.okta.default",
        },
    },
    {
        "id": "a_later_variant",
        "entry": "coredns-log",
        "body": {"variant": "filebeat.coredns_log.json"},
        "expected_stored": {
            "fetcher": None,
            "match": {"field": "data_stream.dataset", "operator": "equals", "value": "coredns.log"},
            "meta_schema": "meta/elastic/ecs",
            "origin": "receiver",
            "variant": "filebeat.coredns_log.json",
        },
    },
]


class CreateAgainstTheContractCase(TypedDict):
    id: str
    entry: str
    body: dict[str, Any]
    expected_status: int


CREATE_AGAINST_THE_CONTRACT_CASES: list[CreateAgainstTheContractCase] = [
    {"id": "listed_variant", "entry": "cisco-asa", "body": {}, "expected_status": 201},
    {
        "id": "default_variant_not_listed",
        "entry": "o365-metrics-entra-alerts",
        "body": {},
        "expected_status": 409,
    },
    {
        "id": "chosen_variant_not_listed",
        "entry": "coredns-log",
        "body": {"variant": "filebeat.coredns_log.json"},
        "expected_status": 409,
    },
]


class CreateFromCatalogueRejectCase(TypedDict):
    id: str
    entry: str
    body: dict[str, Any]
    expected_response: dict[str, Any]


CREATE_FROM_CATALOGUE_REJECT_CASES: list[CreateFromCatalogueRejectCase] = [
    {
        "id": "an_intake_the_entry_does_not_arrive_by",
        "entry": "okta",
        "body": {"intake": "receiver"},
        "expected_response": {
            "code": "catalogue_error",
            "message": "catalogue entry 'okta' does not arrive by 'receiver'; it arrives by "
            "beats, fetcher",
            "errors": [],
        },
    },
    {
        "id": "a_variant_the_entry_does_not_compile_to",
        "entry": "cisco-asa",
        "body": {"variant": "filebeat.cisco_asa.json"},
        "expected_response": {
            "code": "catalogue_error",
            "message": "catalogue entry 'cisco-asa' has no variant 'filebeat.cisco_asa.json'; "
            "it has filebeat.cisco_asa.default",
            "errors": [],
        },
    },
    {
        "id": "an_entry_not_in_the_catalogue",
        "entry": "not-a-source",
        "body": {},
        "expected_response": {
            "code": "catalogue_error",
            "message": "no catalogue entry named 'not-a-source'",
            "errors": [],
        },
    },
    {
        "id": "a_name_that_is_not_a_label",
        "entry": "cisco-asa",
        "body": {"name": "Edge_Firewall"},
        "expected_response": {
            "code": "validation_error",
            "message": "Source name 'Edge_Firewall' must be a Kubernetes DNS-1123 label starting "
            "with a letter (use '-' instead of '_'): a source-bound app such as a transform or a "
            "fetcher is deployed as one instance per source, named for the source, so the name "
            "becomes an Argo Application and a set of Kubernetes object names",
            "errors": [],
        },
    },
    {
        "id": "the_retired_transform_field",
        "entry": "cisco-asa",
        "body": {"transform": "default"},
        "expected_response": {
            "code": "validation_error",
            "message": "1 validation error(s)",
            "errors": [
                {
                    "field": "entries.0.transform",
                    "message": "Extra inputs are not permitted",
                    "code": "extra_forbidden",
                }
            ],
        },
    },
]
