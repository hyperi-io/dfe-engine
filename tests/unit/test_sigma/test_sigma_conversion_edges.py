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


@pytest.mark.parametrize(
    ("selection", "expected"),
    [
        pytest.param(
            {
                "field!@#$%": "value!@#$%",
                "field\\with\\backslash": "value\\with\\backslash",
                'field"with"quotes': 'value"with"quotes',
            },
            "`field!@#$%` ILIKE 'value!@#$\\\\%' AND `field\\\\with\\\\backslash` ILIKE "
            "'value\\\\\\\\with\\\\\\\\backslash' AND `field\"with\"quotes`='value\"with\"quotes'",
            id="special_characters",
        ),
        pytest.param(
            {"field1": "unicode ♠♣♥♦", "field2": "éèêë"},
            "field1='unicode ♠♣♥♦' AND field2='éèêë'",
            id="unicode",
        ),
        pytest.param(
            {"empty": "", "one": " ", "three": "   ", "newline": "\n", "tab": "\t"},
            "empty='' AND one=' ' AND three='   ' AND newline='\n' AND tab='\t'",
            id="whitespace",
        ),
        pytest.param(
            {"field1": None, "field2": "None"},
            "isNull(field1) AND field2='None'",
            id="null_and_the_string_none",
        ),
        pytest.param(
            {"Image": ["a.exe", None]},
            "Image='a.exe' OR isNull(Image)",
            id="null_among_values",
        ),
        pytest.param(
            {"process.parent.name": "v1", "ProcessName": "v2", "processName": "v3"},
            "process.parent.name='v1' AND ProcessName='v2' AND processName='v3'",
            id="dotted_and_mixed_case_fields",
        ),
        pytest.param({"null": "v", "not": "w"}, "`null`='v' AND `not`='w'", id="keyword_fields"),
        pytest.param({"a" * 255: "b" * 1000}, f"{'a' * 255}='{'b' * 1000}'", id="long"),
    ],
)
def test_an_unusual_detection_converts_to_the_where_a_hunt_runs(selection: dict, expected: str):
    assert convert_detection_to_where(_rule(selection)) == expected


def test_a_thousand_conditions_join_into_one_conjunction():
    where = convert_detection_to_where(_rule({f"field{i}": f"value{i}" for i in range(1000)}))

    assert where.count(" AND ") == 999
    assert "field0='value0'" in where
    assert "field999='value999'" in where


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
        "((field1='value1' AND (field2 ILIKE '%value2%' OR field2 ILIKE '%value3%'))"
        " OR (field3 ILIKE '%value4' OR field3 ILIKE '%value5'))"
        " AND (NOT ((field4 ILIKE 'prefix1%' OR field4 ILIKE 'prefix2%')"
        " OR match(field5, 'pattern.*')))"
    )


def test_a_rule_without_a_logsource_is_refused():
    with pytest.raises(SigmaLogsourceError, match="must have a log source"):
        convert_detection_to_where({"title": "t", "detection": {"s": {"f": "v"}, "condition": "s"}})


def test_an_unknown_level_is_refused():
    with pytest.raises(SigmaLevelError, match="not a valid Sigma rule level"):
        convert_detection_to_where(_rule({"f": "v"}) | {"level": "invalid_level"})
