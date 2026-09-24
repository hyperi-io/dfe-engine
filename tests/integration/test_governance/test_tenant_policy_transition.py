#  Project:      dfe-engine
#  File:         tests/integration/test_governance/test_tenant_policy_transition.py
#  Purpose:      A table that gains or loses _org_id trades its tenant policy, live
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Real-ClickHouse proof that the tenant fence follows a table's ``_org_id``.

Both policy kinds are named for the table, so a table that gains ``_org_id``
already holds its deny policy under the name the tenant policy wants. Asserted on
the server's own ``system.row_policies`` and on what a pinned tenant user reads.
"""

import hashlib
import uuid

import pytest

from dfe_engine.governance.ch.models import ChTier, tenant_policy_name
from dfe_engine.governance.ch.reconciler import fence_tables
from dfe_engine.governance.ch.render import render_pinned_user, render_tier

pytestmark = pytest.mark.integration

TABLE = "events"


def _filter(ch_client, db: str) -> str | None:
    rows = ch_client.query(
        "SELECT select_filter FROM system.row_policies "
        "WHERE database = {db:String} AND table = {tbl:String} AND short_name = {name:String}",
        parameters={"db": db, "tbl": TABLE, "name": tenant_policy_name(db, TABLE)},
    ).result_rows
    return rows[0][0] if rows else None


def _count_as(ch_params: dict, user: str, password: str, table_fqn: str) -> int:
    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host=ch_params["host"],
        port=ch_params["port"],
        username=user,
        password=password,
        secure=ch_params["secure"],
    )
    try:
        return int(client.query(f"SELECT count() FROM {table_fqn}").result_rows[0][0])
    finally:
        client.close()


@pytest.fixture
def world(ch_client, ch_params):
    """A uid-scoped database, analyst tier and org-pinned user, dropped afterwards."""
    try:
        ch_client.command("SET SQL_current_tenant_id = 'probe'")
    except Exception:
        pytest.skip("server does not allow the SQL_ custom-settings prefix")

    uid = uuid.uuid4().hex[:10]
    db = f"dfe_test_polswap_{uid}"
    tier = ChTier(name=f"polswap{uid}", kind="analyst", grants=[f"SELECT ON {db}.*"])
    user = f"dfe_test_polswap_user_{uid}"
    password = uuid.uuid4().hex

    ch_client.command(f"CREATE DATABASE {db}")
    ch_client.command(
        f"CREATE TABLE {db}.{TABLE} (payload String) ENGINE = MergeTree ORDER BY payload"
    )
    for stmt in render_tier(tier):
        ch_client.command(stmt)
    try:
        yield {"db": db, "tier": tier, "user": user, "password": password}
    finally:
        for stmt in (
            f"DROP USER IF EXISTS `{user}`",
            f"DROP ROW POLICY IF EXISTS `{tenant_policy_name(db, TABLE)}` ON `{db}`.`{TABLE}`",
            f"DROP ROLE IF EXISTS `{tier.role()}`",
            f"DROP DATABASE IF EXISTS {db}",
        ):
            try:
                ch_client.command(stmt)
            except Exception:
                pass


def _pin(ch_client, w: dict, org: str) -> None:
    pw_hash = hashlib.sha256(w["password"].encode()).hexdigest()
    for stmt in render_pinned_user(w["user"], pw_hash, tier_role=w["tier"].role(), org_ids=[org]):
        ch_client.command(stmt)


def test_a_table_that_gains_org_id_trades_its_deny_policy_for_the_tenant_one(
    ch_client, ch_params, world
):
    db = world["db"]
    fence_tables(ch_client, tiers=[world["tier"]], database=db)
    assert _filter(ch_client, db) == "0"

    ch_client.command(f"ALTER TABLE {db}.{TABLE} ADD COLUMN _org_id String")
    ch_client.command(
        f"INSERT INTO {db}.{TABLE} (payload, _org_id) VALUES ('a1', 'orga'), ('a2', 'orga'), "
        "('b1', 'orgb')"
    )
    _pin(ch_client, world, "orga")
    table_fqn = f"{db}.{TABLE}"
    assert _count_as(ch_params, world["user"], world["password"], table_fqn) == 0

    fence_tables(ch_client, tiers=[world["tier"]], database=db)

    assert "_org_id" in (_filter(ch_client, db) or "")
    assert _count_as(ch_params, world["user"], world["password"], table_fqn) == 2


def test_a_table_that_loses_org_id_is_denied_again(ch_client, ch_params, world):
    db = world["db"]
    ch_client.command(f"ALTER TABLE {db}.{TABLE} ADD COLUMN _org_id String")
    fence_tables(ch_client, tiers=[world["tier"]], database=db)
    assert "_org_id" in (_filter(ch_client, db) or "")

    ch_client.command(f"ALTER TABLE {db}.{TABLE} DROP COLUMN _org_id")
    fence_tables(ch_client, tiers=[world["tier"]], database=db)

    assert _filter(ch_client, db) == "0"
