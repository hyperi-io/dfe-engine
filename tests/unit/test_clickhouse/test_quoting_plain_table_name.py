#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_quoting_plain_table_name.py
#  Purpose:      A configured table name is one identifier, or two joined by a single dot
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""``plain_table_name`` admits letters, digits and ``_`` around at most one dot.

Anything else in a name spliced into SQL could name a table function, close the
statement or comment out the rest of it. The address below is in the
documentation range (RFC 5737).
"""

import pytest

from dfe_engine.clickhouse.quoting import plain_table_name


@pytest.mark.parametrize(
    ("name", "parts"),
    [
        ("detection", ("", "detection")),
        ("dfe.detection", ("dfe", "detection")),
        ("_hunt_db.results_2", ("_hunt_db", "results_2")),
        (" dfe.detection\n", ("dfe", "detection")),
    ],
)
def test_a_plain_table_name_splits_into_database_and_table(name, parts):
    assert plain_table_name(name) == parts


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "FUNCTION url('http://203.0.113.9/x', 'JSONEachRow') -- .x",
        "dfe.detection results",
        "dfe.detection()",
        "dfe.det'ection",
        'dfe."detection"',
        "`dfe`.`detection`",
        "dfe.detection -- trailing",
        "dfe.detection/*x*/",
        "dfe.detection; DROP TABLE dfe.main",
        "cluster.dfe.detection",
        ".detection",
        "dfe.",
        "dfe.1detection",
        "dfe.detection-results",
    ],
)
def test_anything_else_is_not_a_plain_table_name(name):
    with pytest.raises(ValueError, match="is not a table name"):
        plain_table_name(name)
