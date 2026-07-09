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
