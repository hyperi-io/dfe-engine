"""Live-ClickHouse tests for the pinned-setting tenant-isolation invariant.

The tenant axis is ONE shared role and one RESTRICTIVE row policy per table whose
predicate reads the caller's pinned ``SQL_current_tenant_id``:

  | user                              | must see                  |
  |-----------------------------------|---------------------------|
  | no tenant role (platform)         | ALL rows                  |
  | pinned to one org                 | only that org             |
  | pinned to two orgs                | both orgs, nothing else   |
  | pin override in query text        | hard error (code 452)     |
  | tenant role but NO pin            | hard error (fails closed) |

The first row is a specific ClickHouse property (restrictive-only policies,
PR #34596) and the 452 is a settings-constraint behaviour - neither can be
checked without a real server, so this exercises the actual render DDL end to
end. Everything is created under a unique ``uid`` suffix and dropped in
teardown, so it is safe on a shared cluster.

Requires the server to allow the ``SQL_`` custom-settings prefix
(``custom_settings_prefixes``); the fixture skips where it cannot tell.

No mocks (project policy): a real CH, real users, real row policies.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest

from dfe_engine.governance.ch.models import TENANT_ROLE, tenant_policy_name
from dfe_engine.governance.ch.render import render_pinned_user, render_tenant_axis

from .conftest import count_as, drop_safely

pytestmark = pytest.mark.integration

_PW = "Rbac_Test_Pw_9f3b2c"  # nosec - throwaway test-user password on a PET cluster
_PW_HASH = hashlib.sha256(_PW.encode()).hexdigest()


def _prefix_allowed(ch_client) -> bool:
    """Whether the server accepts SQL_-prefixed custom settings."""
    try:
        ch_client.command("SET SQL_current_tenant_id = 'probe'")
        return True
    except Exception:
        return False


@pytest.fixture
def tenant_world(ch_client, ch_params):
    """Build a table + the shared tenant axis + four pinned-shape users.

    The tenant role and policies are SHARED objects with fixed names, so on a
    shared cluster this fixture must not run concurrently with another instance of
    itself; the uid-scoped db keeps the data isolated regardless.
    """
    if not _prefix_allowed(ch_client):
        pytest.skip("server does not allow the SQL_ custom-settings prefix")

    uid = uuid.uuid4().hex[:10]
    db = f"dfe_test_pin_{uid}"
    table = "events"
    table_fqn = f"{db}.{table}"
    ops_table = "ops"  # tenant-reachable table with NO _org_id (the deny class)
    ops_fqn = f"{db}.{ops_table}"
    org_a = f"orga{uid}"
    org_b = f"orgb{uid}"

    u_pinned = f"dfe_test_pinned_{uid}"  # pinned to org_a
    u_open = f"dfe_test_open_{uid}"  # no tenant role -> unrestricted
    u_multi = f"dfe_test_multi_{uid}"  # pinned to BOTH orgs
    u_nopin = f"dfe_test_nopin_{uid}"  # tenant role but no pin -> must error
    users = [u_pinned, u_open, u_multi, u_nopin]
    tier_role = f"dfe_test_tier_{uid}_role"

    def _drop_all() -> None:
        for u in users:
            drop_safely(ch_client, f"DROP USER IF EXISTS {u}")
        drop_safely(
            ch_client,
            f"DROP ROW POLICY IF EXISTS {tenant_policy_name(db, table)} ON {table_fqn}",
        )
        drop_safely(
            ch_client,
            f"DROP ROW POLICY IF EXISTS {tenant_policy_name(db, ops_table)} ON {ops_fqn}",
        )
        drop_safely(ch_client, f"DROP ROLE IF EXISTS {tier_role}")
        drop_safely(ch_client, f"DROP DATABASE IF EXISTS {db}")
        # The shared tenant role is left in place: a real deployment owns it, and
        # only the uid-scoped policy created here referenced it.

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

            ch_client.command(
                f"CREATE TABLE {ops_fqn} (id UInt32, note String) ENGINE = MergeTree() ORDER BY id"
            )
            ch_client.command(f"INSERT INTO {ops_fqn} (id, note) VALUES (1,'x'),(2,'y')")

            ch_client.command(f"CREATE ROLE IF NOT EXISTS {tier_role}")
            ch_client.command(f"GRANT SELECT ON {table_fqn} TO {tier_role}")
            # The tier can read the non-_org_id table too (mirrors SELECT ON dfe.*),
            # so only the deny policy stops the pinned user reading it.
            ch_client.command(f"GRANT SELECT ON {ops_fqn} TO {tier_role}")

            # Real code under test: the shared axis (with a deny policy for the
            # non-_org_id table) + the pinned-user shapes.
            for stmt in render_tenant_axis([(db, table)], deny_tables=[(db, ops_table)]):
                ch_client.command(stmt)
            for stmt in render_pinned_user(
                u_pinned, _PW_HASH, tier_role=tier_role, org_ids=[org_a]
            ):
                ch_client.command(stmt)
            for stmt in render_pinned_user(u_open, _PW_HASH, tier_role=tier_role, org_ids=[]):
                ch_client.command(stmt)
            for stmt in render_pinned_user(
                u_multi, _PW_HASH, tier_role=tier_role, org_ids=[org_a, org_b]
            ):
                ch_client.command(stmt)
            for stmt in render_pinned_user(u_nopin, _PW_HASH, tier_role=tier_role, org_ids=[]):
                ch_client.command(stmt)
            # The misconfiguration case: tenant role granted, no pin.
            ch_client.command(f"GRANT {TENANT_ROLE} TO {u_nopin}")
            # Granted roles must be DEFAULT so they are active on login (else the
            # row policy that targets the role would not apply).
            for u in users:
                ch_client.command(f"ALTER USER {u} DEFAULT ROLE ALL")
        except Exception:
            # A provisioning failure is a real break: skipping here would report
            # the isolation invariant as proven when nothing had been created.
            _drop_all()
            raise

        yield {
            "params": ch_params,
            "table_fqn": table_fqn,
            "u_pinned": u_pinned,
            "u_open": u_open,
            "u_multi": u_multi,
            "u_nopin": u_nopin,
            "org_b": org_b,
            "ops_fqn": ops_fqn,
        }
    finally:
        _drop_all()


class TestPinnedTenantIsolation:
    def test_pinned_user_sees_only_its_org(self, tenant_world):
        w = tenant_world
        assert count_as(w["params"], w["u_pinned"], _PW, w["table_fqn"]) == 3

    def test_unrestricted_user_sees_all_rows(self, tenant_world):
        """No tenant role -> no policy applies -> ALL rows.

        This is the exact restrictive-only property the platform axis relies on;
        if CH flipped to default-deny this would be 0 and the test would fail.
        """
        w = tenant_world
        assert count_as(w["params"], w["u_open"], _PW, w["table_fqn"]) == 5

    def test_multi_org_pin_sees_both_orgs(self, tenant_world):
        """A comma-joined pin spans orgs - the double-grant zero-rows trap of the
        per-org-role design cannot occur here."""
        w = tenant_world
        assert count_as(w["params"], w["u_multi"], _PW, w["table_fqn"]) == 5

    def test_text_override_is_a_hard_error(self, tenant_world):
        """THE attack: the SQL author pins a different tenant in query text.

        The READONLY user setting rejects the whole query (code 452,
        SETTING_CONSTRAINT_VIOLATION) rather than returning the other org's rows.
        """
        import clickhouse_connect

        w = tenant_world
        client = clickhouse_connect.get_client(
            host=w["params"]["host"],
            port=w["params"]["port"],
            username=w["u_pinned"],
            password=_PW,
            secure=w["params"]["secure"],
        )
        try:
            with pytest.raises(Exception, match=r"SETTING_CONSTRAINT_VIOLATION|452"):
                client.query(
                    f"SELECT count() FROM {w['table_fqn']} "
                    f"SETTINGS SQL_current_tenant_id = '{w['org_b']}'"
                )
        finally:
            client.close()

    def test_tenant_role_without_pin_fails_closed(self, tenant_world):
        """A holder with no pin errors on read - misconfiguration is loud, and it
        never degrades to unrestricted."""
        import clickhouse_connect

        w = tenant_world
        client = clickhouse_connect.get_client(
            host=w["params"]["host"],
            port=w["params"]["port"],
            username=w["u_nopin"],
            password=_PW,
            secure=w["params"]["secure"],
        )
        try:
            with pytest.raises(Exception):
                client.query(f"SELECT count() FROM {w['table_fqn']}")
        finally:
            client.close()

    def test_pinned_user_denied_a_non_org_id_table(self, tenant_world):
        """A tenant-reachable table with no _org_id is default-deny (USING 0), not
        read in full - the hunt-orchestration-table class (dfe-engine#157)."""
        w = tenant_world
        assert count_as(w["params"], w["u_pinned"], _PW, w["ops_fqn"]) == 0

    def test_unrestricted_user_still_reads_the_non_org_id_table(self, tenant_world):
        """The deny targets the tenant role only - a platform user is untouched."""
        w = tenant_world
        assert count_as(w["params"], w["u_open"], _PW, w["ops_fqn"]) == 2
