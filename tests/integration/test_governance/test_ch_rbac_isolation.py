"""Live-ClickHouse tests for the load-bearing org-isolation invariant (spec 5.2).

The whole two-axis RBAC design rests on RESTRICTIVE-ONLY row policies:

  | user's roles on the table        | must see        |
  |----------------------------------|-----------------|
  | none (unrestricted)              | ALL rows        |
  | one org role                     | only that org   |
  | two org roles (accidental)       | 0 (fails closed)|

That "no policy applies -> see ALL" behaviour is a specific ClickHouse property
(PR #34596); it cannot be checked without a real cluster, so this exercises the
actual `render_org_role` DDL end to end. Everything is created under a unique
`uid` suffix and dropped in teardown, so it is safe on a shared cluster.

These build their DDL by hand, so they prove ClickHouse's behaviour rather than
the engine's. That the engine actually GRANTS an org role is proven separately,
in `test_ch_rbac_reconcile.py`.

No mocks (project policy): a real CH, real users, real row policies.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest

from dfe_engine.governance.ch.models import org_policy_name, org_role_name
from dfe_engine.governance.ch.render import render_org_role

from .conftest import count_as, drop_safely

pytestmark = pytest.mark.integration

_PW = "Rbac_Test_Pw_9f3b2c"  # nosec - throwaway test-user password on a PET cluster
_PW_HASH = hashlib.sha256(_PW.encode()).hexdigest()


def _count_as(params: dict, user: str, table_fqn: str) -> int:
    """Row count for a hand-built test user, which all share one password."""
    return count_as(params, user, _PW, table_fqn)


@pytest.fixture(scope="module")
def rbac_world(admin_client, conn_params):
    """Build a table + two org roles/policies + three users on the real cluster.

    Yields the handles the tests need. Tears everything down in a finally so a
    failed assertion never leaves objects on a shared cluster.
    """
    ch_client = admin_client
    ch_params = conn_params
    uid = uuid.uuid4().hex[:10]
    db = f"dfe_test_rbac_{uid}"
    table = "events"
    table_fqn = f"{db}.{table}"
    org_a = f"orga{uid}"  # org id value == org name (unique -> unique CH objects)
    org_b = f"orgb{uid}"
    u_scoped = f"dfe_test_scoped_{uid}"  # holds org_a role only
    u_open = f"dfe_test_open_{uid}"  # no org role -> unrestricted
    u_double = f"dfe_test_double_{uid}"  # holds BOTH org roles -> fails closed

    users = [u_scoped, u_open, u_double]
    roles = [org_role_name(org_a), org_role_name(org_b)]
    policies = [
        (org_policy_name(org_a, db, table), table_fqn),
        (org_policy_name(org_b, db, table), table_fqn),
    ]

    def _drop_all() -> None:
        for u in users:
            drop_safely(ch_client, f"DROP USER IF EXISTS {u}")
        for policy, tgt in policies:
            drop_safely(ch_client, f"DROP ROW POLICY IF EXISTS {policy} ON {tgt}")
        for r in roles:
            drop_safely(ch_client, f"DROP ROLE IF EXISTS {r}")
        drop_safely(ch_client, f"DROP DATABASE IF EXISTS {db}")

    try:
        try:
            ch_client.command(f"CREATE DATABASE IF NOT EXISTS {db}")
            ch_client.command(
                f"CREATE TABLE {table_fqn} "
                "(_org_id LowCardinality(String), payload String) "
                "ENGINE = MergeTree() ORDER BY _org_id"
            )
            # 3 rows for org_a, 2 for org_b (5 total).
            ch_client.command(
                f"INSERT INTO {table_fqn} (_org_id, payload) VALUES "
                f"('{org_a}','a1'),('{org_a}','a2'),('{org_a}','a3'),"
                f"('{org_b}','b1'),('{org_b}','b2')"
            )

            # Real code under test: role + RESTRICTIVE row policy per org.
            for stmt in render_org_role(org_a, [org_a], [(db, table)]):
                ch_client.command(stmt)
            for stmt in render_org_role(org_b, [org_b], [(db, table)]):
                ch_client.command(stmt)

            # Users: all can SELECT the table; org roles decide visibility.
            for u in users:
                ch_client.command(
                    f"CREATE USER IF NOT EXISTS {u} IDENTIFIED WITH sha256_hash BY '{_PW_HASH}'"
                )
                ch_client.command(f"GRANT SELECT ON {table_fqn} TO {u}")
            ch_client.command(f"GRANT {org_role_name(org_a)} TO {u_scoped}")
            ch_client.command(f"GRANT {org_role_name(org_a)} TO {u_double}")
            ch_client.command(f"GRANT {org_role_name(org_b)} TO {u_double}")
            # Granted roles must be DEFAULT so they are active on login (else the
            # row policy that targets the role would not apply).
            for u in (u_scoped, u_double):
                ch_client.command(f"ALTER USER {u} DEFAULT ROLE ALL")
        except Exception:
            # A provisioning failure is a real break: skipping here reported the
            # isolation invariant as proven when nothing had been created.
            _drop_all()
            raise

        yield {
            "params": ch_params,
            "table_fqn": table_fqn,
            "u_scoped": u_scoped,
            "u_open": u_open,
            "u_double": u_double,
        }
    finally:
        _drop_all()


class TestRestrictiveOnlyIsolation:
    def test_org_scoped_user_sees_only_its_rows(self, rbac_world):
        """A user holding one org role sees only that org's rows (3 of 5)."""
        n = _count_as(rbac_world["params"], rbac_world["u_scoped"], rbac_world["table_fqn"])
        assert n == 3

    def test_unrestricted_user_sees_all_rows(self, rbac_world):
        """A user with NO org role is targeted by no policy -> sees ALL rows.

        This is the exact restrictive-only property the whole design relies on;
        if CH flipped to default-deny this would be 0 and the test would fail.
        """
        n = _count_as(rbac_world["params"], rbac_world["u_open"], rbac_world["table_fqn"])
        assert n == 5

    def test_double_org_grant_fails_closed(self, rbac_world):
        """Two restrictive policies AND together -> an impossible predicate -> 0.

        Proves an accidental double-org grant leaks nothing (fails closed).
        """
        n = _count_as(rbac_world["params"], rbac_world["u_double"], rbac_world["table_fqn"])
        assert n == 0
