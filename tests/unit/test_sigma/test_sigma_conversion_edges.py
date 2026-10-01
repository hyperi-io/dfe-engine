#  Project:      dfe-engine
#  File:         tests/unit/test_sigma/test_sigma_conversion_edges.py
#  Purpose:      Edge-case Sigma detections through the live conversion path
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Unusual Sigma detections convert to the WHERE a hunt actually runs.

Every case goes through ``propagation.convert_detection_to_where``, the path that
turns a selected catalogue rule into a DFE rule.
"""

import pytest
from sigma.exceptions import SigmaLevelError, SigmaLogsourceError

from dfe_engine.sigma.propagation import convert_detection_to_where


def _rule(selection: dict, condition: str = "selection", **detection) -> dict:
    return {
        "title": "Edge Case",
        "logsource": {"product": "windows", "service": "security"},
        "detection": {"selection": selection, **detection, "condition": condition},
        "level": "medium",
    }


def test_special_characters_quote_the_field_and_escape_the_value():
    where = convert_detection_to_where(
        _rule(
            {
                "field!@#$%": "value!@#$%",
                "field\\with\\backslash": "value\\with\\backslash",
                'field"with"quotes': 'value"with"quotes',
            }
        )
    )

    assert "`field!@#$%` = 'value!@#$%'" in where
    assert "`field\\\\with\\\\backslash` = 'value\\\\with\\\\backslash'" in where
    assert '`field"with"quotes` = \'value"with"quotes\'' in where


def test_unicode_values_pass_through_unchanged():
    where = convert_detection_to_where(_rule({"field1": "unicode ♠♣♥♦", "field2": "éèêë"}))

    assert "field1 = 'unicode ♠♣♥♦'" in where
    assert "field2 = 'éèêë'" in where


def test_whitespace_inside_a_value_is_kept_exactly():
    where = convert_detection_to_where(
        _rule({"empty": "", "one": " ", "three": "   ", "newline": "\n", "tab": "\t"})
    )

    assert "empty = ''" in where
    assert "one = ' '" in where
    assert "three = '   '" in where
    assert "newline = '\n'" in where
    assert "tab = '\t'" in where


def test_null_is_a_null_test_and_the_string_none_is_a_value():
    where = convert_detection_to_where(_rule({"field1": None, "field2": "None"}))

    assert where == "(field1 is null AND field2 = 'None')"


def test_null_among_one_fields_values_is_a_null_test_in_the_or():
    """str() of a SigmaNull is its object address, which also changes every run."""
    rule = _rule({"Image": ["a.exe", None]})

    first = convert_detection_to_where(rule)

    assert first == "(Image = 'a.exe' OR Image is null)"
    assert convert_detection_to_where(rule) == first


def test_dotted_and_mixed_case_field_names_stay_as_written():
    where = convert_detection_to_where(
        _rule({"process.parent.name": "v1", "ProcessName": "v2", "processName": "v3"})
    )

    assert "process.parent.name = 'v1'" in where
    assert "ProcessName = 'v2'" in where
    assert "processName = 'v3'" in where


def test_a_field_named_like_a_keyword_is_quoted():
    where = convert_detection_to_where(_rule({"null": "v", "not": "w"}))

    assert where == "(`null` = 'v' AND `not` = 'w')"


def test_a_long_field_name_and_value_convert():
    where = convert_detection_to_where(_rule({"a" * 255: "b" * 1000}))

    assert where == f"{'a' * 255} = '{'b' * 1000}'"


def test_a_thousand_conditions_join_into_one_conjunction():
    where = convert_detection_to_where(_rule({f"field{i}": f"value{i}" for i in range(1000)}))

    assert where.count(" AND ") == 999
    assert "field0 = 'value0'" in where
    assert "field999 = 'value999'" in where


def test_nested_conditions_keep_their_grouping():
    where = convert_detection_to_where(
        _rule(
            {"field1": "value1"},
            condition="(selection and selection2 or selection3) and not 1 of filter*",
            selection2={"field2|contains": ["value2", "value3"]},
            selection3={"field3|endswith": ["value4", "value5"]},
            filter1={"field4|startswith": ["prefix1", "prefix2"]},
            filter2={"field5|re": "pattern.*"},
        )
    )

    assert where == (
        "(((field1 = 'value1' AND (field2 ILIKE '%value2%' OR field2 ILIKE '%value3%'))"
        " OR (field3 ILIKE '%value4' OR field3 ILIKE '%value5'))"
        " AND (NOT ((field4 ILIKE 'prefix1%' OR field4 ILIKE 'prefix2%')"
        " OR match(field5, 'pattern.*'))))"
    )


def test_a_rule_without_a_logsource_is_refused():
    with pytest.raises(SigmaLogsourceError, match="must have a log source"):
        convert_detection_to_where({"title": "t", "detection": {"s": {"f": "v"}, "condition": "s"}})


def test_an_unknown_level_is_refused():
    with pytest.raises(SigmaLevelError, match="not a valid Sigma rule level"):
        convert_detection_to_where(_rule({"f": "v"}) | {"level": "invalid_level"})
