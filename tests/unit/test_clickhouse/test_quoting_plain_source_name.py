#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_quoting_plain_source_name.py
#  Purpose:      A source table name may carry '-', and nothing else a table name may not
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""``plain_source_name`` admits a source label such as ``cisco-ios`` in either part.

A source table is named for its source label, a DNS-1123 label joined with ``-``.
Both parts are backtick-quoted wherever the name is spliced, so ``-`` is safe there;
anything that could name a table function, close the statement or comment out the
rest of it is still refused. A results table keeps :func:`plain_table_name`. The
address below is in the documentation range (RFC 5737).
"""

import pytest

from dfe_engine.clickhouse.quoting import plain_source_name, plain_table_name


@pytest.mark.parametrize(
    ("name", "parts"),
    [
        ("cisco-ios", ("", "cisco-ios")),
        ("dfe.cisco-ios", ("dfe", "cisco-ios")),
        ("dfe.windows-audit_sigma", ("dfe", "windows-audit_sigma")),
        ("tenant-a.cisco-meraki", ("tenant-a", "cisco-meraki")),
        ("dfe.main", ("dfe", "main")),
        (" _hunt_db.results_2\n", ("_hunt_db", "results_2")),
    ],
)
def test_a_source_name_splits_into_database_and_table(name, parts):
    assert plain_source_name(name) == parts


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "url('http://203.0.113.9/x', 'JSONEachRow')",
        "dfe.cisco-ios extra",
        "dfe.cisco-ios()",
        "dfe.cisco-ios) -- x",
        "dfe.cisco-ios -- trailing",
        "dfe.cisco-ios/*x*/",
        "dfe.cisco-ios; DROP TABLE dfe.main",
        "dfe.cis'co-ios",
        'dfe."cisco-ios"',
        "`dfe`.`cisco-ios`",
        "cluster.dfe.cisco-ios",
        ".cisco-ios",
        "dfe.",
        "dfe.-cisco",
        "-dfe.cisco",
        "dfe.1cisco",
    ],
)
def test_anything_else_is_not_a_source_name(name):
    with pytest.raises(ValueError, match="is not a table name"):
        plain_source_name(name)


def test_a_results_table_still_refuses_a_hyphen():
    with pytest.raises(ValueError, match="is not a table name"):
        plain_table_name("dfe.detection-results")
