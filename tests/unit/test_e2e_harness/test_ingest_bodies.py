#  Project:      dfe-engine
#  File:         tests/unit/test_e2e_harness/test_ingest_bodies.py
#  Purpose:      The two batched bodies the live suite posts, and the tokens it counts them by
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The batch the live suite sends, checked without a deployment.

A batched POST proves one row per element, so the body has to be the shape the
receiver splits and every element has to be countable on its own. Neither is
visible from the live run: a body the receiver declines to split lands one row
and reads as the very bug the case exists to catch.
"""

from __future__ import annotations

import json

from tests.e2e.conftest import batch_body, ndjson_body
from tests.e2e.flows.test_flows import BATCH_SIZE, _batch, _element


class TestTheBatchedBodies:
    def test_the_array_body_is_one_top_level_array(self) -> None:
        body = batch_body(_batch("flowmarker"))
        assert body.startswith(b"[")
        assert len(json.loads(body)) == BATCH_SIZE

    def test_the_ndjson_body_is_one_complete_value_per_line(self) -> None:
        lines = ndjson_body(_batch("flowmarker")).splitlines()
        assert len(lines) == BATCH_SIZE
        for line in lines:
            assert isinstance(json.loads(line), dict)

    def test_the_ndjson_body_carries_more_than_one_line(self) -> None:
        # The receiver only splits a body with a second line, so a batch of one
        # would take the single-event path and prove nothing.
        assert BATCH_SIZE > 1

    def test_neither_body_carries_a_newline_inside_a_record(self) -> None:
        # A record serialised across lines would shred into fragments on the
        # NDJSON path, each of them a DLQ entry rather than a row.
        assert b"\n" not in batch_body(_batch("flowmarker"))


class TestTheElementTokens:
    def test_every_element_carries_its_own_token(self) -> None:
        records = _batch("flowmarker")
        assert len(records) == BATCH_SIZE
        assert len({json.dumps(record) for record in records}) == BATCH_SIZE

    def test_no_token_is_a_substring_of_another(self) -> None:
        # The per-element count is a _raw LIKE, so one token inside another would
        # count a neighbour's row and hide a dropped element.
        tokens = [_element("flowmarker", index) for index in range(BATCH_SIZE)]
        for token in tokens:
            assert [other for other in tokens if token in other] == [token]

    def test_each_token_reaches_the_body_it_is_counted_in(self) -> None:
        body = batch_body(_batch("flowmarker"))
        for index in range(BATCH_SIZE):
            assert _element("flowmarker", index).encode() in body
