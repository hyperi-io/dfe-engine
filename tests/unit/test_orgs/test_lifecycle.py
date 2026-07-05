#  Project:      dfe-engine
#  File:         tests/unit/test_orgs/test_lifecycle.py
#  Purpose:      Unit tests for OrgLifecycleManager
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Unit tests for OrgLifecycleManager.

Uses a real OrgRegistry backed by tmp_path. The HyperDX client is passed as None
- the non-fatal pattern means those operations simply skip when it is absent.
Per-org ClickHouse isolation moved to governance.ch.ChRbacReconciler (row
policies on _org_id), so the lifecycle manager no longer touches ClickHouse.
"""

from __future__ import annotations

import pytest

from dfe_engine.orgs.lifecycle import OrgLifecycleManager
from dfe_engine.orgs.registry import OrgRegistry


@pytest.fixture
def registry(tmp_path):
    return OrgRegistry(tmp_path / "orgs")


@pytest.fixture
def manager(registry):
    """Lifecycle manager with no HyperDX client (CH isolation is elsewhere now)."""
    return OrgLifecycleManager(registry, hyperdx_client=None)


@pytest.mark.asyncio
async def test_create_org_basic(manager, registry):
    org = await manager.create_org(
        "acme",
        org_ids=["acme", "acme-sub"],
        display_name="Acme Corp",
        admin_id="admin",
    )

    assert org.name == "acme"
    assert org.display_name == "Acme Corp"
    assert org.org_ids == ["acme", "acme-sub"]
    assert org.enabled is True

    stored = registry.get("acme")
    assert stored is not None
    assert stored.name == "acme"


@pytest.mark.asyncio
async def test_create_org_defaults(manager, registry):
    org = await manager.create_org("minimal", admin_id="admin")

    assert org.name == "minimal"
    assert org.org_ids == []
    assert org.display_name == ""


@pytest.mark.asyncio
async def test_create_org_duplicate_raises(manager):
    await manager.create_org("acme", admin_id="admin")
    with pytest.raises(ValueError, match="already exists"):
        await manager.create_org("acme", admin_id="admin")


@pytest.mark.asyncio
async def test_delete_org(manager, registry):
    await manager.create_org("acme", admin_id="admin")
    await manager.delete_org("acme", admin_id="admin")

    assert registry.get("acme") is None


@pytest.mark.asyncio
async def test_delete_org_not_found_raises(manager):
    with pytest.raises(KeyError):
        await manager.delete_org("nonexistent", admin_id="admin")


class _FakeHyperDX:
    """Task-sanctioned HyperDX fake (no live HyperDX): records the calls the
    lifecycle manager issues so a test can assert what it DID."""

    def __init__(self) -> None:
        self.connections: list[dict] = []  # create_connection kwargs
        self.removed: list[tuple[str, str]] = []  # (team_api_key, email)
        self.created_teams: list[str] = []  # names passed to create_team

    async def create_team(self, name: str) -> str:
        self.created_teams.append(name)
        return f"team-{len(self.created_teams)}"  # distinct id per creation

    async def get_team_api_key(self, team_id: str) -> str:
        return f"api-key-{team_id}"

    async def create_connection(self, **kwargs) -> str:
        self.connections.append(kwargs)
        return "conn-1"

    async def remove_member(self, team_api_key: str, email: str) -> bool:
        self.removed.append((team_api_key, email))
        return True

    async def delete_team(self, team_id: str) -> bool:
        return True


def _file_secrets(tmp_path):
    from dfe_engine.secrets import build_secrets
    from dfe_engine.settings import SecretsSettings

    return build_secrets(SecretsSettings(provider="file", path=str(tmp_path / "secrets")))


def _default_conn_config():
    from dfe_engine.connections.config import ConnectionConfig
    from dfe_engine.connections.models import ClickHouseConnection

    return ConnectionConfig(
        connections={
            "default": ClickHouseConnection(
                name="default", host="ch-host", port=8123, database="dfe", user="default"
            )
        }
    )


@pytest.mark.asyncio
async def test_create_org_connection_uses_tenant_reader_and_tenant_setting(registry, tmp_path):
    """Task A: the org's HyperDX connection authenticates as the SHARED
    dfe_tenant_reader (secret ch/fixed/dfe_tenant_reader) and carries the org's
    DFE_current_tenant_id = comma-joined org_ids - NOT a per-group dfe_grp_<org> user."""
    secrets = _file_secrets(tmp_path)
    secrets.put("ch/fixed/dfe_tenant_reader", "reader-pw")  # where the reconciler stores it

    hdx = _FakeHyperDX()
    manager = OrgLifecycleManager(
        registry,
        hyperdx_client=hdx,
        connection_config=_default_conn_config(),
        secrets_store=secrets,
    )

    await manager.create_org("acme", org_ids=["acme", "globex"], admin_id="admin")

    assert len(hdx.connections) == 1
    conn = hdx.connections[0]
    assert conn["name"] == "acme"
    assert conn["user"] == "dfe_tenant_reader"  # shared fixed reader
    assert conn["password"] == "reader-pw"  # sourced from ch/fixed/dfe_tenant_reader
    assert conn["settings"] == {"DFE_current_tenant_id": "acme,globex"}
    assert conn["host"] == "ch-host"  # network coords from the `default` connection
    assert conn["port"] == 8123
    assert conn["database"] == "dfe"


@pytest.mark.asyncio
async def test_ga_posture_uses_one_shared_team(registry, tmp_path):
    """Task B GA default (per_group False): every org joins ONE shared team
    (ga_team_name), created once and reused - never a duplicate per org."""
    hdx = _FakeHyperDX()
    manager = OrgLifecycleManager(
        registry,
        hyperdx_client=hdx,
        connection_config=_default_conn_config(),
        secrets_store=_file_secrets(tmp_path),
        ga_team_name="dfe",
    )

    org_a = await manager.create_org("acme", org_ids=["acme"], admin_id="admin")
    org_b = await manager.create_org("globex", org_ids=["globex"], admin_id="admin")

    # ONE team created ("dfe"), reused by the second org.
    assert hdx.created_teams == ["dfe"]
    assert org_a.hyperdx_team_id == org_b.hyperdx_team_id == "team-1"
    # Still one connection per org (isolation is the tenant setting, not the team).
    assert [c["name"] for c in hdx.connections] == ["acme", "globex"]


@pytest.mark.asyncio
async def test_per_group_posture_uses_per_org_team(registry, tmp_path):
    """Task B post-GA (per_group True): each org gets its own customer-<org> team."""
    hdx = _FakeHyperDX()
    manager = OrgLifecycleManager(
        registry,
        hyperdx_client=hdx,
        connection_config=_default_conn_config(),
        secrets_store=_file_secrets(tmp_path),
        per_group=True,
    )

    await manager.create_org("acme", org_ids=["acme"], admin_id="admin")
    await manager.create_org("globex", org_ids=["globex"], admin_id="admin")

    assert hdx.created_teams == ["customer-acme", "customer-globex"]


@pytest.mark.asyncio
async def test_revoke_member_issues_hyperdx_removal(registry, monkeypatch):
    """5c.4: an account-disable / group-member-removal propagates a HyperDX team
    revoke via the lifecycle seam."""
    registry.create("acme", org_ids=["acme"])
    registry.update("acme", hyperdx_team_api_key_env="HDX_KEY_ACME")
    monkeypatch.setenv("HDX_KEY_ACME", "team-api-key")

    hdx = _FakeHyperDX()
    manager = OrgLifecycleManager(registry, hyperdx_client=hdx)

    issued = await manager.revoke_member("acme", "user@corp.com")

    assert issued is True
    assert hdx.removed == [("team-api-key", "user@corp.com")]


@pytest.mark.asyncio
async def test_revoke_member_without_team_key_is_noop(registry):
    registry.create("acme", org_ids=["acme"])  # no hyperdx_team_api_key_env stored
    hdx = _FakeHyperDX()
    manager = OrgLifecycleManager(registry, hyperdx_client=hdx)

    issued = await manager.revoke_member("acme", "user@corp.com")

    assert issued is False
    assert hdx.removed == []
