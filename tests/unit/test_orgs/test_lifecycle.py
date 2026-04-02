#  Project:      dfe-engine
#  File:         tests/unit/test_orgs/test_lifecycle.py
#  Purpose:      Unit tests for OrgLifecycleManager
#  Language:     Python
#
#  License:      FSL-1.1-ALv2
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Unit tests for OrgLifecycleManager.

Uses a real OrgRegistry backed by tmp_path.  CH provisioner and HyperDX
client are passed as None — the non-fatal pattern means operations simply
skip when they are absent.
"""

from __future__ import annotations

import pytest

from dfe_engine.orgs.lifecycle import OrgLifecycleManager
from dfe_engine.orgs.registry import OrgRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def registry(tmp_path):
    return OrgRegistry(tmp_path / "orgs")


@pytest.fixture
def manager(registry):
    """Lifecycle manager with no CH provisioner or HyperDX client."""
    return OrgLifecycleManager(registry, ch_provisioner=None, hyperdx_client=None)


# ---------------------------------------------------------------------------
# create_org
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_org_basic(manager, registry):
    org = await manager.create_org(
        "acme",
        org_ids=["acme", "acme-sub"],
        display_name="Acme Corp",
        dedicated_database=False,
        admin_id="admin",
    )

    assert org.name == "acme"
    assert org.display_name == "Acme Corp"
    assert org.org_ids == ["acme", "acme-sub"]
    assert org.enabled is True
    assert org.dedicated_database is False
    assert org.database_name == ""

    # Persisted in registry
    stored = registry.get("acme")
    assert stored is not None
    assert stored.name == "acme"


@pytest.mark.asyncio
async def test_create_org_defaults(manager, registry):
    org = await manager.create_org("minimal", admin_id="admin")

    assert org.name == "minimal"
    assert org.org_ids == []
    assert org.display_name == ""
    assert org.dedicated_database is False


@pytest.mark.asyncio
async def test_create_org_dedicated_db_no_provisioner(manager, registry):
    """When dedicated_database=True but no CH provisioner, org is still updated."""
    org = await manager.create_org(
        "bigcorp",
        dedicated_database=True,
        admin_id="admin",
    )

    assert org.dedicated_database is True
    # No provisioner means database_name stays empty
    assert org.database_name == ""


@pytest.mark.asyncio
async def test_create_org_duplicate_raises(manager):
    await manager.create_org("acme", admin_id="admin")
    with pytest.raises(ValueError, match="already exists"):
        await manager.create_org("acme", admin_id="admin")


# ---------------------------------------------------------------------------
# delete_org
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_org(manager, registry):
    await manager.create_org("acme", admin_id="admin")
    await manager.delete_org("acme", admin_id="admin")

    assert registry.get("acme") is None


@pytest.mark.asyncio
async def test_delete_org_not_found_raises(manager):
    with pytest.raises(KeyError):
        await manager.delete_org("nonexistent", admin_id="admin")


@pytest.mark.asyncio
async def test_delete_org_dedicated_db_no_provisioner(manager, registry):
    """Dedicated DB org can be deleted without a provisioner (skip deprovision)."""
    await manager.create_org("acme", dedicated_database=True, admin_id="admin")
    # Should not raise
    await manager.delete_org("acme", admin_id="admin")
    assert registry.get("acme") is None


# ---------------------------------------------------------------------------
# toggle_dedicated_db
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_toggle_dedicated_db_off_requires_confirm(manager):
    await manager.create_org("acme", admin_id="admin")

    with pytest.raises(ValueError, match="confirm_merge"):
        await manager.toggle_dedicated_db(
            "acme", enabled=False, confirm_merge=False, admin_id="admin"
        )


@pytest.mark.asyncio
async def test_toggle_dedicated_db_off_with_confirm(manager, registry):
    # Start with dedicated_database=True via direct registry update
    await manager.create_org("acme", dedicated_database=True, admin_id="admin")

    org = await manager.toggle_dedicated_db(
        "acme", enabled=False, confirm_merge=True, admin_id="admin"
    )

    assert org.dedicated_database is False
    stored = registry.get("acme")
    assert stored is not None
    assert stored.dedicated_database is False


@pytest.mark.asyncio
async def test_toggle_dedicated_db_on_no_provisioner(manager, registry):
    await manager.create_org("acme", admin_id="admin")

    org = await manager.toggle_dedicated_db("acme", enabled=True, admin_id="admin")

    assert org.dedicated_database is True
    stored = registry.get("acme")
    assert stored is not None
    assert stored.dedicated_database is True


@pytest.mark.asyncio
async def test_toggle_dedicated_db_on_sets_database_name_when_provisioner():
    """When a CH provisioner is present and provision succeeds, database_name is set."""
    import re
    from unittest.mock import MagicMock

    # Build a minimal mock provisioner that simulates success
    provisioner = MagicMock()
    provisioner.provision.return_value = (True, "somepassword")
    provisioner.database_name.side_effect = lambda name: (
        f"dfe_{re.sub(r'[^a-z0-9_]', '_', name.lower())}"
    )
    provisioner.ch_user_name.side_effect = lambda name: (
        f"dfe_org_{re.sub(r'[^a-z0-9_]', '_', name.lower())}"
    )

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        registry = OrgRegistry(Path(tmpdir) / "orgs")
        mgr = OrgLifecycleManager(registry, ch_provisioner=provisioner, hyperdx_client=None)

        await mgr.create_org("acme", admin_id="admin")
        org = await mgr.toggle_dedicated_db("acme", enabled=True, admin_id="admin")

    assert org.dedicated_database is True
    assert org.database_name == "dfe_acme"


@pytest.mark.asyncio
async def test_toggle_dedicated_db_not_found_raises(manager):
    with pytest.raises(KeyError):
        await manager.toggle_dedicated_db("nonexistent", enabled=True, admin_id="admin")
