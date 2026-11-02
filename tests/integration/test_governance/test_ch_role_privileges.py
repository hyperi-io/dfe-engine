#  Project:      dfe-engine
#  File:         tests/integration/test_governance/test_ch_role_privileges.py
#  Purpose:      A reconciled role holds exactly its configured privileges, on a real ClickHouse
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Each reconcile replaces a role's privileges, read back with ``SHOW GRANTS``."""

import uuid

import clickhouse_connect
import pytest

from dfe_engine.governance.ch.models import ChServiceRole
from dfe_engine.governance.ch.reconciler import ChRbacReconciler

pytestmark = pytest.mark.integration


@pytest.fixture
def role(ch_client):
    """A uid-named service role, dropped afterwards."""
    name = f"probe{uuid.uuid4().hex[:8]}"
    yield name
    ch_client.command(f"DROP ROLE IF EXISTS `dfe_{name}_role`")
    ch_client.command(f"DROP USER IF EXISTS `dfe_{name}_u`")


def _grants(ch_client, role: str) -> list[str]:
    rows = ch_client.query(f"SHOW GRANTS FOR `dfe_{role}_role`").result_rows
    return sorted(str(row[0]) for row in rows)


def _reconcile(ch_client, db: str, role: str, grants: list[str]):
    return ChRbacReconciler(ch_client, database=db).reconcile_service_roles(
        [ChServiceRole(name=role, grants=grants)]
    )


def test_a_grant_dropped_from_the_role_is_revoked(ch_client, clickhouse_test_database, role):
    db = clickhouse_test_database
    _reconcile(ch_client, db, role, ["SELECT ON {db}.*", "INSERT ON {db}.*"])

    result = _reconcile(ch_client, db, role, ["SELECT ON {db}.*"])

    assert result.errors == []
    assert _grants(ch_client, role) == [f"GRANT SELECT ON {db}.* TO dfe_{role}_role"]


def test_a_refused_grant_list_still_revokes_what_the_role_held(
    ch_client, clickhouse_test_database, role
):
    """ClickHouse refuses the whole replacing GRANT over one bad entry, so it falls back."""
    db = clickhouse_test_database
    _reconcile(ch_client, db, role, ["SELECT ON {db}.*", "INSERT ON {db}.*"])

    result = _reconcile(ch_client, db, role, ["SELECT ON {db}.*", "FROBNICATE ON {db}.*"])

    assert len(result.errors) == 2, result.errors
    assert _grants(ch_client, role) == [f"GRANT SELECT ON {db}.* TO dfe_{role}_role"]


def test_a_view_granted_on_its_own_stays_readable_through_the_database_grant(
    ch_client, ch_params, clickhouse_test_database, role
):
    """dfe-schemas grants each view to the reader alone, and the replace drops that line."""
    db = clickhouse_test_database
    ch_client.command(f"CREATE VIEW `{db}`.v AS SELECT 1 AS one")
    _reconcile(ch_client, db, role, ["SELECT ON {db}.*"])
    ch_client.command(f"GRANT SELECT ON `{db}`.v TO `dfe_{role}_role`")
    user, password = f"dfe_{role}_u", uuid.uuid4().hex
    ch_client.command(f"CREATE USER `{user}` IDENTIFIED WITH plaintext_password BY '{password}'")
    ch_client.command(f"GRANT `dfe_{role}_role` TO `{user}`")

    _reconcile(ch_client, db, role, ["SELECT ON {db}.*"])

    assert _grants(ch_client, role) == [f"GRANT SELECT ON {db}.* TO dfe_{role}_role"]
    reader = clickhouse_connect.get_client(
        host=ch_params["host"], port=ch_params["port"], username=user, password=password
    )
    try:
        assert reader.query(f"SELECT one FROM `{db}`.v").result_rows == [(1,)]
    finally:
        reader.close()
