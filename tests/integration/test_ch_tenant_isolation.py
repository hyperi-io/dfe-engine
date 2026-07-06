#  Project:      dfe-engine
#  File:         tests/integration/test_ch_tenant_isolation.py
#  Purpose:      Live-CH proof of the custom-settings tenant isolation model
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Live-ClickHouse proof of the custom-settings tenant-isolation model.

Formalises .tmp/fable-review/ch-proof/proof.py against a REAL ClickHouse: apply
the ACTUAL rendered DDL from ``governance.ch.render`` (the fixed ``dfe_tenant_reader``
with readonly + ``SQL_current_tenant_id`` CHANGEABLE_IN_READONLY, plus the ONE
``has(splitByChar(...))`` row policy), then prove per-tenant isolation both via the
driver ``settings=`` param (the TenantScopedClient injection path) AND via the real
TenantScopedClient wrapper:

    acme        -> only acme rows (3)
    acme,globex -> both orgs      (5)   [multi-org]
    unset / ''  -> 0              (fail closed)
    unknown     -> 0

Connects to the LOCAL dev CH daemon (localhost:18123, default/proofadmin) which
has ``<custom_settings_prefixes>SQL_</custom_settings_prefixes>`` configured. Skips
gracefully if that daemon is unreachable. No mocks: real CH, real user, real policy.

Deliberately ONE test function: the model's user (``dfe_tenant_reader``) and table
are FIXED-named shared objects, and the suite always runs under xdist ``-n 4``. A
single test runs on exactly one worker, so the seed/teardown never races a sibling
worker re-dropping the same fixed-named user mid-query.
"""

from __future__ import annotations

import hashlib

import pytest

from dfe_engine.connections.tenant import TenantScopedClient
from dfe_engine.governance.ch.render import render_fixed_users, render_tenant_policies

pytestmark = pytest.mark.integration

_HOST, _PORT = "localhost", 18123
_ADMIN_USER, _ADMIN_PW = "default", "proofadmin"
_READER_PW = "readerpw"  # nosec - throwaway local-daemon test password
_READER_PW_HASH = hashlib.sha256(_READER_PW.encode()).hexdigest()


def _client(username: str, password: str):
    import clickhouse_connect

    return clickhouse_connect.get_client(
        host=_HOST, port=_PORT, username=username, password=password
    )


def _admin_or_skip():
    """Admin client, or skip the test if the local daemon is unreachable."""
    try:
        a = _client(_ADMIN_USER, _ADMIN_PW)
        a.command("SELECT 1")
        return a
    except Exception as exc:  # any connect error -> skip, never fail
        pytest.skip(f"local CH daemon {_HOST}:{_PORT} unreachable: {exc}")


def _seed_and_apply(a) -> None:
    """Seed a 2-org table + apply the REAL rendered reader/policy DDL."""
    a.command("CREATE DATABASE IF NOT EXISTS dfe")
    # dfe_hunts is in the reader's grant set (SELECT ON dfe_hunts.*); create it so
    # the real rendered grants never trip on a missing database.
    a.command("CREATE DATABASE IF NOT EXISTS dfe_hunts")
    a.command("DROP TABLE IF EXISTS dfe.events")
    a.command(
        "CREATE TABLE dfe.events (_org_id String, _timestamp DateTime DEFAULT now(), "
        "msg String) ENGINE = MergeTree ORDER BY (_org_id, _timestamp)"
    )
    # Inserted by admin BEFORE the policy exists: 3 acme rows, 2 globex rows.
    a.command(
        "INSERT INTO dfe.events (_org_id, msg) VALUES "
        "('acme','a1'),('acme','a2'),('acme','a3'),('globex','g1'),('globex','g2')"
    )
    # The REAL code under test: the fixed reader + the ONE tenant policy.
    a.command("DROP ROW POLICY IF EXISTS dfe_tenant_filter ON dfe.events")
    a.command("DROP USER IF EXISTS dfe_tenant_reader")
    for stmt in render_fixed_users({"dfe_tenant_reader": _READER_PW_HASH}):
        a.command(stmt)
    for stmt in render_tenant_policies([("dfe", "events")]):
        a.command(stmt)


def _teardown(a) -> None:
    for stmt in (
        "DROP ROW POLICY IF EXISTS dfe_tenant_filter ON dfe.events",
        "DROP USER IF EXISTS dfe_tenant_reader",
        "DROP TABLE IF EXISTS dfe.events",
    ):
        try:
            a.command(stmt)
        except Exception:  # teardown is best-effort
            pass


def _count_via_settings(setting: str | None) -> int:
    """count() as dfe_tenant_reader with the driver settings= param (or unset)."""
    r = _client("dfe_tenant_reader", _READER_PW)
    try:
        kwargs = {} if setting is None else {"settings": {"SQL_current_tenant_id": setting}}
        return int(r.query("SELECT count() FROM dfe.events", **kwargs).result_rows[0][0])
    finally:
        r.close()


def _count_via_tenant_client(org_ids: list[str]) -> int:
    """count() as dfe_tenant_reader wrapped in the real TenantScopedClient."""
    r = _client("dfe_tenant_reader", _READER_PW)
    try:
        scoped = TenantScopedClient(r, org_ids=org_ids)
        return int(scoped.query("SELECT count() FROM dfe.events").result_rows[0][0])
    finally:
        r.close()


def test_tenant_isolation_end_to_end() -> None:
    """Seed -> apply rendered DDL -> assert per-tenant isolation + fail-closed."""
    admin = _admin_or_skip()
    try:
        _seed_and_apply(admin)

        # --- driver settings= path (the TenantScopedClient injection path) -----
        assert _count_via_settings("acme") == 3  # only acme
        assert _count_via_settings("globex") == 2  # only globex
        assert _count_via_settings("acme,globex") == 5  # multi-org comma list
        assert _count_via_settings(None) == 0  # unset -> fail closed
        assert _count_via_settings("") == 0  # empty -> fail closed
        assert _count_via_settings("nosuchorg") == 0  # unknown -> 0

        # --- the real TenantScopedClient wrapper -------------------------------
        assert _count_via_tenant_client(["acme"]) == 3
        assert _count_via_tenant_client(["acme", "globex"]) == 5
        assert _count_via_tenant_client([]) == 0  # no orgs -> '' -> fail closed
        assert _count_via_tenant_client(["nosuchorg"]) == 0
    finally:
        _teardown(admin)
