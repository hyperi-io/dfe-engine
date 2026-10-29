#  Project:      dfe-engine
#  File:         tests/unit/test_services/test_sampler_receiver_parity.py
#  Purpose:      The sampler's match filter selects exactly the records the receiver routes
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A source preview must never show a record the receiver would not route to that source.

Each operator runs the same records two ways: through the sampler's SQL filter in
clickhouse-local, over the landing table's ``_json`` column type, and through the
source's compiled receiver row in scalo.rules under the receiver's CEL Fast
profile. The two must select the same records.

Two shapes are left out because the stored column has lost what the receiver
reads. ClickHouse stores ``1.50`` as a float and reads it back as ``1.5``, while
the receiver reads a number as the sender wrote it. And it stores no path for an
empty object, which the receiver's ``exists`` holds for, so the row reads as one
without the field.
"""

import json
import subprocess
from typing import Any

import pytest
from scalo.rules import CompileOptions
from scalo.rules import RuleSet as ScaloRuleSet

from dfe_engine.services.models.receiver import RULE_TARGETS, RuleSet
from dfe_engine.services.schema.json_promotion_service import match_condition
from dfe_engine.services.source_routing import RECEIVER_RULES_PROFILE, source_rules
from dfe_engine.source.models import Source
from tests.support.clickhouse_local import clickhouse_local

FIELD = "data_stream.dataset"

# The landing table's _json column, as the common header declares it.
_JSON_TYPE = "JSON(max_dynamic_paths=2048)"

# Every shape the field can take that a test treats differently.
RECORDS: dict[int, dict[str, Any]] = {
    1: {"data_stream": {"dataset": "cisco_ios.log"}},
    2: {"data_stream": {"dataset": "Cisco_IOS.log"}},
    3: {"data_stream": {"dataset": "cisco_ios.log.debug"}},
    4: {"data_stream": {"dataset": "x.cisco_ios.log"}},
    5: {"agent": {"type": "filebeat"}},
    6: {"data_stream": {"namespace": "acme"}},
    7: {"data_stream": {"dataset": None}},
    8: {"data_stream": {"dataset": ""}},
    9: {"data_stream": {"dataset": True}},
    10: {"data_stream": {"dataset": ["cisco_ios.log"]}},
    11: {"data_stream": {"dataset": {"name": "cisco_ios.log"}}},
    12: {"data_stream": {"dataset": []}},
    13: {"data_stream": {"dataset": "2"}},
    14: {"data_stream": {"dataset": "true"}},
}

# Operands that land on a case difference, a prefix, a suffix, a boolean's text, and the
# text ClickHouse renders an array or an object as, which the receiver never reads.
VALUES = [
    "cisco_ios.log",
    "Cisco",
    "ios",
    ".log",
    "true",
    "['cisco_ios.log']",
    '{"name":"cisco_ios.log"}',
    "[",
    "{",
    "]",
    "}",
]


def _sampled(operator: str, value: str) -> set[int]:
    """The record ids the sampler's filter selects, run in clickhouse-local."""
    where, params = match_condition(FIELD, value, match_operator=operator)
    rows = "\n".join(json.dumps({"id": rid, "_json": record}) for rid, record in RECORDS.items())
    query = (
        f"SELECT id FROM format(JSONEachRow, 'id UInt32, _json {_JSON_TYPE}', $${rows}$$) "
        f"WHERE {where} ORDER BY id FORMAT TSV"
    )
    result = subprocess.run(
        [
            *clickhouse_local(),
            "--query",
            query,
            *(f"--param_{name}={bound}" for name, bound in params.items()),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return {int(line) for line in result.stdout.split()}


def _routed(operator: str, value: str) -> set[int]:
    """The record ids the source's receiver row labels with the source, in scalo.rules."""
    source = Source.model_validate(
        {"source": "probe", "match": {"field": FIELD, "operator": operator, "value": value}}
    )
    rule_set = RuleSet(targets=list(RULE_TARGETS), rules=source_rules(source))
    compiled = ScaloRuleSet.compile(
        rule_set.model_dump(mode="json", exclude_defaults=True),
        CompileOptions(profile=RECEIVER_RULES_PROFILE),
    )
    return {
        rid
        for rid, record in RECORDS.items()
        if compiled.evaluate(record).value("_source") == "probe"
    }


@pytest.mark.parametrize(
    "operator", ["equals", "not_equals", "includes", "starts_with", "ends_with"]
)
def test_a_text_test_samples_the_records_the_receiver_routes(operator):
    differences = {
        value: {"sampled": _sampled(operator, value), "routed": _routed(operator, value)}
        for value in VALUES
    }
    disagreeing = {
        value: ids for value, ids in differences.items() if ids["sampled"] != ids["routed"]
    }
    assert not disagreeing, f"{operator}: the preview and the receiver disagree on {disagreeing}"


def test_exists_samples_the_records_the_receiver_routes():
    assert _sampled("exists", "") == _routed("exists", "")


def test_the_records_tell_the_operators_apart():
    # Guards the parity above against a record set every filter treats alike.
    assert _routed("includes", "ios") == {1, 3, 4}
    assert _routed("exists", "") == {1, 2, 3, 4, 9, 10, 11, 12, 13, 14}
    assert _routed("not_equals", "cisco_ios.log") == set(RECORDS) - {1}
