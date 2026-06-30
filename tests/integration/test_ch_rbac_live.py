#  Project:      dfe-engine
#  File:         tests/integration/test_ch_rbac_live.py
#  Purpose:      Live ClickHouse test of per-group user (grants+quota+profile)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Integration test against a REAL ClickHouse (no mocks).

Uses the ch_client fixture (settings/.env CH first - e.g. the devex pet cluster -
then DFE_TEST_CH_* env, then a docker fallback, else skip). Proves the per-group
user is created with its GRANTs, SETTINGS PROFILE, and QUOTA - the three controls
the HyperDX app layer cannot enforce. DROPs everything it creates.
"""

from __future__ import annotations

import pytest

from dfe_engine.governance.ch_rbac import GroupChBinding, GroupChProvisioner

_GROUP = "soc-ro"
_USER = "dfe_grp_soc-ro"
_PROFILE = "dfe_grp_soc-ro_profile"
_QUOTA = "dfe_grp_soc-ro_quota"


@pytest.fixture
def provisioned(ch_client):
    """Provision the group's CH identity; DROP it (and on failure) after."""
    binding = GroupChBinding(
        group=_GROUP,
        grants=["SELECT ON system.*"],
        settings={"max_execution_time": 30, "readonly": 1},
        quota={"queries": 1000},
        quota_interval="1 hour",
    )
    # clean slate (a prior failed run may have left objects)
    _drop(ch_client)
    ok, password = GroupChProvisioner(ch_client).provision(binding)
    try:
        yield ch_client, ok, password
    finally:
        _drop(ch_client)


def _drop(client):
    for stmt in (
        f"DROP QUOTA IF EXISTS `{_QUOTA}`",
        f"DROP SETTINGS PROFILE IF EXISTS `{_PROFILE}`",
        f"DROP USER IF EXISTS `{_USER}`",
    ):
        try:
            client.command(stmt)
        except Exception:  # cleanup must never fail the test
            pass


def test_provision_creates_user_grants_profile_quota(provisioned):
    client, ok, password = provisioned
    assert ok
    assert len(password) >= 16

    def _exists(table, name):
        rows = client.query(f"SELECT name FROM system.{table} WHERE name = '{name}'").result_rows
        return bool(rows)

    assert _exists("users", _USER), "group CH user not created"
    assert _exists("settings_profiles", _PROFILE), "settings profile not created"
    assert _exists("quotas", _QUOTA), "quota not created"

    grants = client.query(
        f"SELECT access_type FROM system.grants WHERE user_name = '{_USER}'"
    ).result_rows
    assert grants, "no grants applied to the group user"
