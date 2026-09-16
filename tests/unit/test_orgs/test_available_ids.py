#  Project:      dfe-engine
#  File:         tests/unit/test_orgs/test_available_ids.py
#  Purpose:      The tenant ids the org form suggests come from the data, not a guess
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

from __future__ import annotations

import pytest

from dfe_engine.orgs.available_ids import OrgIdDiscoveryError, available_org_ids


class FakeClickHouse:
    """Answers the table lookup first, then the union of distinct ids."""

    def __init__(self, tables: list[str], ids: list[str], fail_on: str = "") -> None:
        self.tables = tables
        self.ids = ids
        self.fail_on = fail_on
        self.calls: list[str] = []

    def execute(self, sql: str, parameters=None, settings=None):
        self.calls.append(sql)
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("clickhouse is down")
        if "system.columns" in sql:
            return [(t,) for t in self.tables]
        return [(i,) for i in self.ids]


def test_the_ids_come_back_from_every_table_that_carries_them():
    ch = FakeClickHouse(["filebeat", "syslog"], ["acme", "globex"])

    assert available_org_ids(ch, database="dfe") == ["acme", "globex"]


def test_every_table_carrying_org_id_is_in_the_union():
    ch = FakeClickHouse(["filebeat", "syslog"], ["acme"])

    available_org_ids(ch, database="dfe")

    union = ch.calls[1]
    assert "`dfe`.`filebeat`" in union
    assert "`dfe`.`syslog`" in union
    assert "UNION DISTINCT" in union


def test_a_database_with_no_tenant_tables_asks_nothing_further():
    ch = FakeClickHouse([], [])

    assert available_org_ids(ch, database="dfe") == []
    assert len(ch.calls) == 1


def test_the_empty_id_is_dropped_rather_than_offered():
    ch = FakeClickHouse(["filebeat"], ["acme"])

    available_org_ids(ch, database="dfe")

    assert "WHERE org_id != ''" in ch.calls[1]


def test_the_suggestion_list_is_bounded():
    ch = FakeClickHouse(["filebeat"], ["acme"])

    available_org_ids(ch, database="dfe", limit=25)

    assert "LIMIT 25" in ch.calls[1]


def test_a_table_name_that_cannot_be_quoted_is_skipped():
    ch = FakeClickHouse(["filebeat", "bad name; DROP TABLE x"], ["acme"])

    available_org_ids(ch, database="dfe")

    union = ch.calls[1]
    assert "DROP TABLE" not in union
    assert "`dfe`.`filebeat`" in union


@pytest.mark.parametrize("database", ["dfe; DROP TABLE x", "dfe.data", "dfe data"])
def test_an_unsafe_database_name_is_refused(database: str):
    with pytest.raises(ValueError, match="unsafe identifier"):
        available_org_ids(FakeClickHouse([], []), database=database)


def test_a_failed_lookup_is_raised_rather_than_reported_as_no_ids():
    ch = FakeClickHouse(["filebeat"], ["acme"], fail_on="system.columns")

    with pytest.raises(OrgIdDiscoveryError, match="cannot list the tenant tables"):
        available_org_ids(ch, database="dfe")


def test_a_failed_union_is_raised_rather_than_reported_as_no_ids():
    ch = FakeClickHouse(["filebeat"], ["acme"], fail_on="ORDER BY org_id")

    with pytest.raises(OrgIdDiscoveryError, match="cannot read the tenant ids"):
        available_org_ids(ch, database="dfe")
