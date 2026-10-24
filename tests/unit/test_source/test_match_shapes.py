#  Project:      dfe-engine
#  File:         tests/unit/test_source/test_match_shapes.py
#  Purpose:      The shapes a Source match and condition may take, and the ones refused
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A source's match is one test, or ``all`` or ``any`` of several, nested. Or it is a ``when``."""

import pytest
from pydantic import ValidationError

from dfe_engine.source.models import (
    MATCH_NESTING_LIMIT,
    Source,
    SourceMatch,
    SourceWriteRequest,
)


def _test(value: str) -> dict:
    return {"field": "data_stream.dataset", "value": value}


def _nested(depth: int) -> dict:
    match = _test("a.log")
    for _ in range(depth):
        match = {"all": [match]}
    return match


class TestMatchShapes:
    def test_a_test_with_no_operator_compares_with_equals(self):
        assert SourceMatch.model_validate(_test("a.log")).operator == "equals"

    @pytest.mark.parametrize(
        ("match", "message"),
        [
            ({"all": [_test("a")], "any": [_test("b")]}, "all or any, not both"),
            ({"field": "f", "all": [_test("a")]}, "do not apply beside all or any"),
            (
                {"any": [{"field": "_source", "operator": "always"}, _test("a")]},
                "stands alone, never in a group",
            ),
            ({"field": "_source", "operator": "always", "negate": True}, "cannot be negated"),
            ({"value": "a"}, "tests a field, or groups tests"),
            ({"field": "f", "operator": "includes"}, "match.value is required"),
            (_nested(MATCH_NESTING_LIMIT + 1), f"at most {MATCH_NESTING_LIMIT} groups deep"),
        ],
    )
    def test_a_shape_the_receiver_cannot_take_is_refused(self, match, message):
        with pytest.raises(ValidationError, match=message):
            SourceMatch.model_validate(match)

    def test_groups_nest_to_the_limit(self):
        assert SourceMatch.model_validate(_nested(MATCH_NESTING_LIMIT)).depth() == (
            MATCH_NESTING_LIMIT
        )

    def test_describe_reads_the_whole_condition(self):
        match = SourceMatch.model_validate(
            {"negate": True, "any": [_test("a.log"), {"field": "agent.type", "operator": "exists"}]}
        )
        assert match.describe() == "not (data_stream.dataset equals a.log or agent.type exists)"

    def test_a_single_condition_dumps_as_it_always_has(self):
        assert SourceMatch.model_validate(_test("a.log")).model_dump(mode="json") == {
            "field": "data_stream.dataset",
            "operator": "equals",
            "value": "a.log",
        }

    def test_a_group_dumps_only_what_it_sets(self):
        match = SourceMatch.model_validate(
            {"all": [_test("a.log"), {**_test("b.log"), "negate": True}]}
        )
        assert match.model_dump(mode="json") == {
            "all": [
                {"field": "data_stream.dataset", "operator": "equals", "value": "a.log"},
                {
                    "field": "data_stream.dataset",
                    "operator": "equals",
                    "value": "b.log",
                    "negate": True,
                },
            ]
        }


class TestWhen:
    def test_match_and_when_are_refused_together(self):
        with pytest.raises(ValidationError, match="match or when"):
            Source.model_validate({"source": "x", "match": _test("a"), "when": "a == 'b'"})

    def test_a_blank_when_is_refused(self):
        with pytest.raises(ValidationError, match="when is empty"):
            SourceWriteRequest.model_validate({"source": "x", "when": "  "})

    def test_a_when_is_a_receiver_origin(self):
        source = Source.model_validate({"source": "x", "when": "a == 'b'"})
        assert source.version().origin == "receiver"

    def test_when_and_fetcher_are_refused_together(self):
        with pytest.raises(ValidationError):
            SourceWriteRequest.model_validate(
                {
                    "source": "x",
                    "when": "a == 'b'",
                    "fetcher": {"source_type": "okta", "config": {}},
                }
            )


class TestBindingShapes:
    @pytest.mark.parametrize(
        ("body", "message"),
        [
            ({"compat_profiles": [{"profile": "a"}, {"profile": "a"}]}, "bound twice"),
            ({"compat_profiles": [{"profile": "Not_An_Id"}]}, "pattern"),
            ({"compat_profiles": [{"profile": "a", "version": "1"}]}, "pattern"),
            ({"compat_profiles": [{"profile": "a", "opt_in": ["x", "x"]}]}, "names a row twice"),
            ({"aliases": {"a": ["x", "x"]}}, "lists an alias twice"),
            ({"aliases": {"a": [" "]}}, "empty alias"),
            ({"aliases": {"Bad": ["x"]}}, "not a compatibility profile id"),
        ],
    )
    def test_a_malformed_binding_or_alias_is_refused(self, body, message):
        with pytest.raises(ValidationError, match=message):
            SourceWriteRequest.model_validate({"source": "x", "match": _test("a"), **body})
