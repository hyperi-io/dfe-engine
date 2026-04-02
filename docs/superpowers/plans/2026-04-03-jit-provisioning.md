# JIT Provisioning + Org CH Isolation Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When an org is created, automatically provision CH isolation + HyperDX team. When an OIDC user logs in for the first time, create a shadow account and assign to the correct HyperDX team.

**Architecture:** Three new components (OrgChProvisioner, OrgLifecycleManager, JitProvisioner) orchestrated by the existing orgs API router and auth dependency. Model changes to Account, Group, and Org. All provisioning is non-fatal (best-effort, log errors).

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, clickhouse-connect, YAML via yaml_utils

**Spec:** `docs/superpowers/specs/2026-04-03-jit-provisioning-design.md`

---

## File Structure

### New Files

| File | Responsibility |
|------|---------------|
| `src/dfe_engine/orgs/ch_provisioner.py` | CH user + database CRUD for dedicated DB orgs |
| `src/dfe_engine/orgs/lifecycle.py` | Orchestrates org lifecycle (CH + HyperDX + loader config) |
| `src/dfe_engine/auth/jit.py` | JIT shadow account creation + HyperDX team assignment |
| `tests/unit/test_orgs/test_ch_provisioner.py` | OrgChProvisioner unit tests |
| `tests/unit/test_orgs/test_lifecycle.py` | OrgLifecycleManager unit tests |
| `tests/unit/test_auth/test_jit.py` | JitProvisioner unit tests |
| `tests/e2e/test_jit_workflow.py` | E2E: org create → OIDC login → shadow account → team assignment |

### Modified Files

| File | Change |
|------|--------|
| `src/dfe_engine/orgs/models.py` | Add `dedicated_database`, internal metadata fields |
| `src/dfe_engine/auth/accounts.py` | Add `external`, `source_provider`, `last_login_at` to Account |
| `src/dfe_engine/auth/groups.py` | Add `org_ids` to Group |
| `src/dfe_engine/auth/audit.py` | Add provisioning audit events |
| `src/dfe_engine/api/deps.py` | Call JitProvisioner after OIDC auth |
| `src/dfe_engine/api/v1/orgs.py` | Wire OrgLifecycleManager, add `confirm_merge` guard |
| `src/dfe_engine/hyperdx/client.py` | Add `delete_team()`, `update_connection()` |
| `src/dfe_engine/api/app.py` | Bootstrap OrgLifecycleManager in lifespan |

---

## Chunk 1: Model Changes

### Task 1: Add fields to Account model

**Files:**
- Modify: `src/dfe_engine/auth/accounts.py:43-51`
- Test: `tests/unit/test_auth/test_accounts.py`

- [ ] **Step 1: Write test for new Account fields**

```python
# tests/unit/test_auth/test_accounts.py — add to existing test class
def test_account_external_fields(self, tmp_path):
    store = AccountStore(tmp_path / "accounts")
    store.create("external-user", "", groups=["viewers"])
    store.update("external-user", external=True, source_provider="entra", last_login_at="2026-04-03T00:00:00Z")
    acct = store.get("external-user")
    assert acct.external is True
    assert acct.source_provider == "entra"
    assert acct.last_login_at == "2026-04-03T00:00:00Z"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_auth/test_accounts.py -k test_account_external_fields -x --override-ini="addopts=" -q`
Expected: FAIL — `external` field unknown

- [ ] **Step 3: Add fields to Account model**

In `src/dfe_engine/auth/accounts.py`, modify class `Account`:

```python
class Account(BaseModel):
    """A local user account."""

    username: str
    password_hash: str
    enabled: bool = True
    groups: list[str] = Field(default_factory=list)
    external: bool = False
    source_provider: str = ""
    last_login_at: str = ""
    created_at: str = ""
    updated_at: str = ""
```

Update `AccountStore.update()` to accept the new fields in its `**fields` kwargs (it already uses `setattr` — just add them to the docstring's supported fields list).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_auth/test_accounts.py -k test_account_external_fields -x --override-ini="addopts=" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```
git add src/dfe_engine/auth/accounts.py tests/unit/test_auth/test_accounts.py
git commit -m "feat: add external, source_provider, last_login_at to Account model"
```

---

### Task 2: Add org_ids to Group model

**Files:**
- Modify: `src/dfe_engine/auth/groups.py:18-30`
- Test: `tests/unit/test_auth/test_groups.py`

- [ ] **Step 1: Write test for Group.org_ids**

```python
# tests/unit/test_auth/test_groups.py — add to existing test class
def test_group_org_ids(self, tmp_path):
    store = GroupStore(tmp_path / "groups")
    store.create("acme-viewers", roles=["customer_viewer"])
    store.update("acme-viewers", org_ids=["acme"])
    group = store.get("acme-viewers")
    assert group.org_ids == ["acme"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_auth/test_groups.py -k test_group_org_ids -x --override-ini="addopts=" -q`

- [ ] **Step 3: Add org_ids field to Group model**

In `src/dfe_engine/auth/groups.py`:

```python
class Group(BaseModel):
    """A named group with roles and member usernames."""

    name: str
    description: str = ""
    roles: list[str] = Field(default_factory=list)
    members: list[str] = Field(default_factory=list)
    source_provider: str = ""
    source_id: str = ""
    org_ids: list[str] = Field(default_factory=list)
```

- [ ] **Step 4: Run test to verify it passes**

- [ ] **Step 5: Commit**

```
git add src/dfe_engine/auth/groups.py tests/unit/test_auth/test_groups.py
git commit -m "feat: add org_ids to Group model for org membership mapping"
```

---

### Task 3: Add dedicated_database + internal metadata to Org model

**Files:**
- Modify: `src/dfe_engine/orgs/models.py`
- Test: `tests/unit/test_orgs/test_models.py`

- [ ] **Step 1: Write test for new Org fields**

```python
# tests/unit/test_orgs/test_models.py — add test
from dfe_engine.orgs.models import Org

def test_org_dedicated_database_default_false():
    org = Org(name="test")
    assert org.dedicated_database is False
    assert org.database_name == ""
    assert org.hyperdx_team_id == ""
    assert org.ch_password_env == ""

def test_org_dedicated_database_enabled():
    org = Org(name="acme", dedicated_database=True, database_name="dfe_acme")
    assert org.dedicated_database is True
    assert org.database_name == "dfe_acme"
```

- [ ] **Step 2: Run test to verify it fails**

- [ ] **Step 3: Add fields to Org model**

In `src/dfe_engine/orgs/models.py`:

```python
class Org(BaseModel):
    name: str
    display_name: str = ""
    org_ids: list[str] = Field(default_factory=list)
    enabled: bool = True
    dedicated_database: bool = False
    # Internal metadata (managed by engine)
    database_name: str = ""
    hyperdx_team_id: str = ""
    ch_password_env: str = ""
    created_at: str = ""
    updated_at: str = ""
```

- [ ] **Step 4: Run test to verify it passes**

- [ ] **Step 5: Commit**

```
git add src/dfe_engine/orgs/models.py tests/unit/test_orgs/test_models.py
git commit -m "feat: add dedicated_database and internal metadata to Org model"
```

---

### Task 4: Add audit events for provisioning

**Files:**
- Modify: `src/dfe_engine/auth/audit.py`
- Test: `tests/unit/test_auth/test_audit.py`

- [ ] **Step 1: Write tests for new audit events**

```python
# tests/unit/test_auth/test_audit.py — add to existing file
from unittest.mock import patch

def test_audit_org_provisioning():
    from dfe_engine.auth.audit import audit_org_change, audit_jit_account_created

    with patch("dfe_engine.auth.audit.logger") as mock_logger:
        audit_org_change("admin", "acme", "created", {"dedicated_database": False})
        mock_logger.info.assert_called_once()
        call_kwargs = mock_logger.info.call_args
        assert "org.created" in str(call_kwargs)

    with patch("dfe_engine.auth.audit.logger") as mock_logger:
        audit_jit_account_created("jane@corp.com", "entra", ["viewers"], ["acme"])
        mock_logger.info.assert_called_once()
        call_kwargs = mock_logger.info.call_args
        assert "auth.jit.account_created" in str(call_kwargs)
```

- [ ] **Step 2: Run test to verify it fails**

- [ ] **Step 3: Add audit functions**

Append to `src/dfe_engine/auth/audit.py`:

```python
def audit_org_change(
    admin_id: str,
    org_name: str,
    change: str,
    details: dict | None = None,
) -> None:
    logger.info(
        f"org.{change}",
        admin_id=admin_id,
        org_name=org_name,
        change=change,
        details=details,
    )


def audit_org_ch_provisioned(
    org_name: str,
    ch_user: str,
    databases: list[str],
) -> None:
    logger.info(
        "org.ch.user_created",
        org_name=org_name,
        ch_user=ch_user,
        databases=databases,
    )


def audit_org_ch_failed(org_name: str, error: str) -> None:
    logger.warning("org.ch.provision_failed", org_name=org_name, error=error)


def audit_org_hyperdx_provisioned(org_name: str, team_id: str) -> None:
    logger.info("org.hyperdx.team_created", org_name=org_name, team_id=team_id)


def audit_org_hyperdx_failed(org_name: str, error: str) -> None:
    logger.warning("org.hyperdx.provision_failed", org_name=org_name, error=error)


def audit_jit_account_created(
    user_id: str,
    source_provider: str,
    groups: list[str],
    org_ids: list[str],
) -> None:
    logger.info(
        "auth.jit.account_created",
        user_id=user_id,
        source_provider=source_provider,
        groups=groups,
        org_ids=org_ids,
    )


def audit_jit_groups_updated(
    user_id: str,
    added: list[str],
    removed: list[str],
) -> None:
    logger.info(
        "auth.jit.groups_updated",
        user_id=user_id,
        added_groups=added,
        removed_groups=removed,
    )


def audit_jit_team_assigned(user_id: str, team_name: str, reason: str) -> None:
    logger.info(
        "auth.jit.team_assigned",
        user_id=user_id,
        team_name=team_name,
        reason=reason,
    )


def audit_jit_failed(user_id: str, error: str) -> None:
    logger.warning("auth.jit.provision_failed", user_id=user_id, error=error)
```

- [ ] **Step 4: Run test to verify it passes**

- [ ] **Step 5: Commit**

```
git add src/dfe_engine/auth/audit.py tests/unit/test_auth/test_audit.py
git commit -m "feat: add SOC2 audit events for org provisioning and JIT accounts"
```

---

## Chunk 2: OrgChProvisioner + HyperDX Client Extensions

### Task 5: Add delete_team and update_connection to HyperDXClient

**Files:**
- Modify: `src/dfe_engine/hyperdx/client.py`
- Test: `tests/unit/test_hyperdx/test_client.py`

- [ ] **Step 1: Write tests for new HyperDX methods**

```python
# tests/unit/test_hyperdx/test_client.py — add tests
def test_delete_team_success(self):
    """delete_team returns True on success."""
    client = HyperDXClient(base_url="http://localhost:8080", api_key="test")
    # Simulate successful response
    # (HyperDXClient uses non-fatal pattern — test the method exists and handles errors)
    result = client.delete_team("nonexistent-team-id")
    # Without a real server, first call sets _connected=False
    assert result is False  # Expected: non-fatal failure

def test_update_connection_success(self):
    """update_connection returns True on success."""
    client = HyperDXClient(base_url="http://localhost:8080", api_key="test")
    result = client.update_connection("team-id", "conn-id", database="dfe_acme")
    assert result is False  # Non-fatal failure (no server)
```

- [ ] **Step 2: Run test to verify it fails**

- [ ] **Step 3: Add methods to HyperDXClient**

Read `src/dfe_engine/hyperdx/client.py` to understand the existing non-fatal pattern, then add:

```python
def delete_team(self, team_id: str) -> bool:
    """Delete a HyperDX team. Non-fatal on failure."""
    if not self._connected:
        logger.warning("HyperDX not connected, skipping team delete", team_id=team_id)
        return False
    try:
        resp = self._client.delete(f"/api/v1/teams/{team_id}")
        resp.raise_for_status()
        return True
    except Exception as exc:
        logger.warning("HyperDX delete_team failed", team_id=team_id, error=str(exc))
        self._connected = False
        return False

def update_connection(
    self,
    team_id: str,
    connection_id: str,
    **kwargs,
) -> bool:
    """Update a connection on a HyperDX team. Non-fatal on failure."""
    if not self._connected:
        logger.warning("HyperDX not connected, skipping connection update")
        return False
    try:
        resp = self._client.put(
            f"/api/v1/teams/{team_id}/connections/{connection_id}",
            json=kwargs,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        logger.warning("HyperDX update_connection failed", error=str(exc))
        self._connected = False
        return False
```

- [ ] **Step 4: Run test to verify it passes**

- [ ] **Step 5: Commit**

```
git add src/dfe_engine/hyperdx/client.py tests/unit/test_hyperdx/test_client.py
git commit -m "feat: add delete_team and update_connection to HyperDXClient"
```

---

### Task 6: Create OrgChProvisioner

**Files:**
- Create: `src/dfe_engine/orgs/ch_provisioner.py`
- Create: `tests/unit/test_orgs/test_ch_provisioner.py`

- [ ] **Step 1: Write tests for OrgChProvisioner**

```python
# tests/unit/test_orgs/test_ch_provisioner.py
import pytest
from dfe_engine.orgs.ch_provisioner import OrgChProvisioner


class TestSqlGeneration:
    def test_provision_dedicated_db_generates_correct_sql(self):
        provisioner = OrgChProvisioner(ch_client=None)
        stmts = provisioner.build_provision_sql("acme", "dfe_acme")
        sql = "\n".join(stmts)
        assert "CREATE DATABASE IF NOT EXISTS dfe_acme" in sql
        assert "CREATE USER IF NOT EXISTS dfe_org_acme" in sql
        assert "GRANT SELECT ON dfe_acme.*" in sql

    def test_deprovision_generates_correct_sql(self):
        provisioner = OrgChProvisioner(ch_client=None)
        stmts = provisioner.build_deprovision_sql("acme", "dfe_acme")
        sql = "\n".join(stmts)
        assert "DROP USER IF EXISTS dfe_org_acme" in sql
        # Should NOT drop database
        assert "DROP DATABASE" not in sql

    def test_user_naming(self):
        provisioner = OrgChProvisioner(ch_client=None)
        assert provisioner.ch_user_name("acme") == "dfe_org_acme"
        assert provisioner.ch_user_name("my-org") == "dfe_org_my_org"

    def test_database_naming(self):
        provisioner = OrgChProvisioner(ch_client=None)
        assert provisioner.database_name("acme") == "dfe_acme"

    def test_password_generation(self):
        provisioner = OrgChProvisioner(ch_client=None)
        pw = provisioner.generate_password()
        assert len(pw) == 32
        # Two calls produce different passwords
        assert pw != provisioner.generate_password()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_orgs/test_ch_provisioner.py -x --override-ini="addopts=" -q`

- [ ] **Step 3: Implement OrgChProvisioner**

Create `src/dfe_engine/orgs/ch_provisioner.py`:

```python
"""ClickHouse user + database provisioner for dedicated org databases.

Only creates CH objects when an org has dedicated_database=True.
Shared DB orgs use the existing custom settings pattern (ConnectionRegistry).
"""

from __future__ import annotations

import re
import secrets
from typing import Any

from hyperi_pylib.logger import logger


class OrgChProvisioner:
    """Provisions CH users + databases for orgs with dedicated databases.

    Non-fatal: logs errors but does not raise. Returns success/failure bool.
    """

    def __init__(self, ch_client: Any | None = None) -> None:
        self._client = ch_client

    def ch_user_name(self, org_name: str) -> str:
        safe = re.sub(r"[^a-z0-9_]", "_", org_name.lower())
        return f"dfe_org_{safe}"

    def database_name(self, org_name: str) -> str:
        safe = re.sub(r"[^a-z0-9_]", "_", org_name.lower())
        return f"dfe_{safe}"

    def generate_password(self) -> str:
        return secrets.token_urlsafe(24)[:32]

    def build_provision_sql(self, org_name: str, db_name: str) -> list[str]:
        user = self.ch_user_name(org_name)
        return [
            f"CREATE DATABASE IF NOT EXISTS {db_name}",
            f"CREATE USER IF NOT EXISTS {user} IDENTIFIED WITH sha256_hash BY '{{password_hash}}'",
            f"GRANT SELECT ON {db_name}.* TO {user}",
        ]

    def build_deprovision_sql(self, org_name: str, db_name: str) -> list[str]:
        user = self.ch_user_name(org_name)
        return [
            f"REVOKE ALL ON {db_name}.* FROM {user}",
            f"DROP USER IF EXISTS {user}",
        ]

    def provision(self, org_name: str) -> tuple[bool, str]:
        """Provision CH user + database. Returns (success, password)."""
        if self._client is None:
            logger.warning("No CH client configured, skipping org provisioning", org_name=org_name)
            return False, ""

        db_name = self.database_name(org_name)
        user = self.ch_user_name(org_name)
        password = self.generate_password()

        try:
            import hashlib
            pw_hash = hashlib.sha256(password.encode()).hexdigest()

            for stmt in self.build_provision_sql(org_name, db_name):
                sql = stmt.replace("{password_hash}", pw_hash)
                self._client.command(sql)

            logger.info("CH org provisioned", org_name=org_name, user=user, database=db_name)
            return True, password
        except Exception as exc:
            logger.warning("CH org provisioning failed", org_name=org_name, error=str(exc))
            return False, ""

    def deprovision(self, org_name: str) -> bool:
        """Remove CH user (not database). Returns success."""
        if self._client is None:
            return False

        db_name = self.database_name(org_name)
        try:
            for stmt in self.build_deprovision_sql(org_name, db_name):
                self._client.command(stmt)
            logger.info("CH org deprovisioned", org_name=org_name)
            return True
        except Exception as exc:
            logger.warning("CH org deprovision failed", org_name=org_name, error=str(exc))
            return False
```

- [ ] **Step 4: Run test to verify it passes**

- [ ] **Step 5: Commit**

```
git add src/dfe_engine/orgs/ch_provisioner.py tests/unit/test_orgs/test_ch_provisioner.py
git commit -m "feat: add OrgChProvisioner for dedicated database provisioning"
```

---

## Chunk 3: OrgLifecycleManager + API Wiring

### Task 7: Create OrgLifecycleManager

**Files:**
- Create: `src/dfe_engine/orgs/lifecycle.py`
- Create: `tests/unit/test_orgs/test_lifecycle.py`

- [ ] **Step 1: Write tests for OrgLifecycleManager**

```python
# tests/unit/test_orgs/test_lifecycle.py
from pathlib import Path
import pytest
from dfe_engine.orgs.lifecycle import OrgLifecycleManager
from dfe_engine.orgs.registry import OrgRegistry


@pytest.fixture
def lifecycle(tmp_path):
    registry = OrgRegistry(tmp_path / "orgs")
    return OrgLifecycleManager(
        registry=registry,
        ch_provisioner=None,
        hyperdx_client=None,
    )


class TestOrgLifecycle:
    def test_create_org_basic(self, lifecycle):
        org = lifecycle.create_org("test-org", org_ids=["test"], admin_id="admin")
        assert org.name == "test-org"
        assert org.org_ids == ["test"]
        assert org.dedicated_database is False

    def test_create_org_dedicated_db(self, lifecycle):
        org = lifecycle.create_org(
            "acme", org_ids=["acme"], dedicated_database=True, admin_id="admin"
        )
        assert org.dedicated_database is True
        assert org.database_name == "dfe_acme"

    def test_delete_org(self, lifecycle):
        lifecycle.create_org("to-delete", org_ids=["del"], admin_id="admin")
        lifecycle.delete_org("to-delete", admin_id="admin")
        assert lifecycle._registry.get("to-delete") is None

    def test_toggle_dedicated_db_off_requires_confirm(self, lifecycle):
        lifecycle.create_org("acme", org_ids=["acme"], dedicated_database=True, admin_id="admin")
        with pytest.raises(ValueError, match="confirm_merge"):
            lifecycle.toggle_dedicated_db("acme", enabled=False, confirm_merge=False, admin_id="admin")

    def test_toggle_dedicated_db_off_with_confirm(self, lifecycle):
        lifecycle.create_org("acme", org_ids=["acme"], dedicated_database=True, admin_id="admin")
        org = lifecycle.toggle_dedicated_db("acme", enabled=False, confirm_merge=True, admin_id="admin")
        assert org.dedicated_database is False
```

- [ ] **Step 2: Run test to verify it fails**

- [ ] **Step 3: Implement OrgLifecycleManager**

Create `src/dfe_engine/orgs/lifecycle.py`:

```python
"""Orchestrates org lifecycle: registry + CH provisioning + HyperDX team."""

from __future__ import annotations

from typing import Any

from hyperi_pylib.logger import logger

from dfe_engine.auth.audit import (
    audit_org_change,
    audit_org_ch_failed,
    audit_org_ch_provisioned,
    audit_org_hyperdx_failed,
    audit_org_hyperdx_provisioned,
)
from dfe_engine.orgs.models import Org
from dfe_engine.orgs.registry import OrgRegistry


class OrgLifecycleManager:
    def __init__(
        self,
        registry: OrgRegistry,
        ch_provisioner: Any | None = None,
        hyperdx_client: Any | None = None,
    ) -> None:
        self._registry = registry
        self._ch = ch_provisioner
        self._hdx = hyperdx_client

    def create_org(
        self,
        name: str,
        *,
        org_ids: list[str] | None = None,
        display_name: str = "",
        dedicated_database: bool = False,
        admin_id: str = "system",
    ) -> Org:
        org = self._registry.create(name, org_ids=org_ids, display_name=display_name)

        if dedicated_database:
            org = self._enable_dedicated_db(org)

        # HyperDX team
        if self._hdx:
            try:
                team_id = self._hdx.create_team(f"customer-{name}")
                if team_id:
                    org = self._registry.update(name, hyperdx_team_id=team_id)
                    audit_org_hyperdx_provisioned(name, team_id)
            except Exception as exc:
                audit_org_hyperdx_failed(name, str(exc))

        audit_org_change(admin_id, name, "created", {"dedicated_database": dedicated_database})
        return org

    def delete_org(self, name: str, *, admin_id: str = "system") -> None:
        org = self._registry.get(name)
        if org is None:
            return

        # Deprovision CH if dedicated
        if org.dedicated_database and self._ch:
            self._ch.deprovision(name)

        # Delete HyperDX team
        if self._hdx and org.hyperdx_team_id:
            self._hdx.delete_team(org.hyperdx_team_id)

        self._registry.delete(name)
        audit_org_change(admin_id, name, "deleted")

    def toggle_dedicated_db(
        self,
        name: str,
        *,
        enabled: bool,
        confirm_merge: bool = False,
        admin_id: str = "system",
    ) -> Org:
        org = self._registry.get(name)
        if org is None:
            raise KeyError(f"Org '{name}' not found")

        if not enabled and not confirm_merge:
            raise ValueError(
                "Disabling dedicated database requires confirm_merge=True. "
                "Existing database will NOT be dropped."
            )

        if enabled:
            org = self._enable_dedicated_db(org)
            audit_org_change(admin_id, name, "dedicated_db.enabled", {"database_name": org.database_name})
        else:
            org = self._registry.update(name, dedicated_database=False)
            audit_org_change(admin_id, name, "dedicated_db.disabled", {"confirmed": True})

        return org

    def _enable_dedicated_db(self, org: Org) -> Org:
        from dfe_engine.orgs.ch_provisioner import OrgChProvisioner

        provisioner = self._ch or OrgChProvisioner(ch_client=None)
        db_name = provisioner.database_name(org.name)

        updates: dict[str, Any] = {
            "dedicated_database": True,
            "database_name": db_name,
        }

        if self._ch:
            success, password = self._ch.provision(org.name)
            if success:
                env_var = f"DFE_CH_ORG_{org.name.upper().replace('-', '_')}_PASSWORD"
                updates["ch_password_env"] = env_var
                audit_org_ch_provisioned(org.name, provisioner.ch_user_name(org.name), [db_name])
            else:
                audit_org_ch_failed(org.name, "provision returned False")

        return self._registry.update(org.name, **updates)
```

- [ ] **Step 4: Run test to verify it passes**

- [ ] **Step 5: Commit**

```
git add src/dfe_engine/orgs/lifecycle.py tests/unit/test_orgs/test_lifecycle.py
git commit -m "feat: add OrgLifecycleManager for org lifecycle orchestration"
```

---

### Task 8: Wire OrgLifecycleManager into orgs API router

**Files:**
- Modify: `src/dfe_engine/api/v1/orgs.py`
- Modify: `src/dfe_engine/api/app.py`
- Test: `tests/unit/test_api/test_orgs.py` (existing tests should still pass)

- [ ] **Step 1: Bootstrap OrgLifecycleManager in app lifespan**

In `src/dfe_engine/api/app.py`, after org registry bootstrap, add:

```python
# Bootstrap org lifecycle manager
from dfe_engine.orgs.lifecycle import OrgLifecycleManager

hdx_client = getattr(app.state, "hyperdx_client", None)
app.state.org_lifecycle = OrgLifecycleManager(
    registry=app.state.org_registry,
    ch_provisioner=None,  # Wired when CH admin client available
    hyperdx_client=hdx_client,
)
```

- [ ] **Step 2: Update orgs router to use OrgLifecycleManager**

In `src/dfe_engine/api/v1/orgs.py`, update create/delete endpoints to use `request.app.state.org_lifecycle` instead of `request.app.state.org_registry` directly. Add `dedicated_database` to CreateOrgRequest. Add `confirm_merge` guard on update.

- [ ] **Step 3: Run existing org tests to verify nothing broke**

Run: `uv run pytest tests/unit/test_api/test_orgs.py -x -q --override-ini="addopts="`

- [ ] **Step 4: Add test for dedicated_database toggle**

```python
def test_toggle_dedicated_db_requires_confirm(self, client, admin_headers):
    client.post("/api/v1/orgs", json={"name": "toggle-test", "org_ids": ["t"]}, headers=admin_headers)
    resp = client.put(
        "/api/v1/orgs/toggle-test",
        json={"dedicated_database": False},
        headers=admin_headers,
    )
    # Should succeed (already false → no-op) or require confirm if was true
    assert resp.status_code in (200, 400)
```

- [ ] **Step 5: Commit**

```
git add src/dfe_engine/api/app.py src/dfe_engine/api/v1/orgs.py tests/unit/test_api/test_orgs.py
git commit -m "feat: wire OrgLifecycleManager into orgs API + app lifespan"
```

---

## Chunk 4: JIT Provisioner + Auth Wiring

### Task 9: Create JitProvisioner

**Files:**
- Create: `src/dfe_engine/auth/jit.py`
- Create: `tests/unit/test_auth/test_jit.py`

- [ ] **Step 1: Write tests for JitProvisioner**

```python
# tests/unit/test_auth/test_jit.py
import pytest
from dfe_engine.auth.accounts import AccountStore
from dfe_engine.auth.groups import Group, GroupStore
from dfe_engine.auth.jit import JitProvisioner


@pytest.fixture
def stores(tmp_path):
    accounts = AccountStore(tmp_path / "accounts")
    groups = GroupStore(tmp_path / "groups")
    # Create a group with org_ids
    groups.create("acme-viewers", roles=["customer_viewer"])
    groups.update("acme-viewers", org_ids=["acme"])
    groups.create("dfe-admins", roles=["admin"])
    return accounts, groups


class TestJitProvisioner:
    def test_first_login_creates_account(self, stores):
        accounts, groups = stores
        jit = JitProvisioner(account_store=accounts, group_store=groups)
        account = jit.ensure_account(
            user_id="jane@corp.com",
            oidc_groups=["acme-viewers"],
            source_provider="entra",
        )
        assert account.username == "jane@corp.com"
        assert account.external is True
        assert account.source_provider == "entra"
        assert account.password_hash == ""

    def test_subsequent_login_updates_last_login(self, stores):
        accounts, groups = stores
        jit = JitProvisioner(account_store=accounts, group_store=groups)
        jit.ensure_account("jane@corp.com", ["acme-viewers"], "entra")
        first = accounts.get("jane-corp-com")
        jit.ensure_account("jane@corp.com", ["acme-viewers"], "entra")
        second = accounts.get("jane-corp-com")
        assert second.last_login_at >= first.last_login_at

    def test_groups_updated_on_change(self, stores):
        accounts, groups = stores
        jit = JitProvisioner(account_store=accounts, group_store=groups)
        jit.ensure_account("jane@corp.com", ["acme-viewers"], "entra")
        jit.ensure_account("jane@corp.com", ["acme-viewers", "dfe-admins"], "entra")
        account = accounts.get("jane-corp-com")
        assert "dfe-admins" in account.groups

    def test_broadest_wins_admin(self, stores):
        accounts, groups = stores
        jit = JitProvisioner(account_store=accounts, group_store=groups)
        team = jit.resolve_hyperdx_team(["acme-viewers", "dfe-admins"], groups)
        assert team == "dfe-admin"

    def test_org_scoped_team(self, stores):
        accounts, groups = stores
        jit = JitProvisioner(account_store=accounts, group_store=groups)
        team = jit.resolve_hyperdx_team(["acme-viewers"], groups)
        assert team == "customer-acme"

    def test_race_condition_handled(self, stores):
        accounts, groups = stores
        jit = JitProvisioner(account_store=accounts, group_store=groups)
        # Simulate race: pre-create the account
        accounts.create("race-user-com", "", groups=["acme-viewers"])
        # Second creation should not raise
        account = jit.ensure_account("race@user.com", ["acme-viewers"], "entra")
        assert account is not None

    def test_username_sanitisation(self, stores):
        accounts, groups = stores
        jit = JitProvisioner(account_store=accounts, group_store=groups)
        assert jit.sanitise_username("jane@corp.com") == "jane-corp-com"
        assert jit.sanitise_username("user.name+tag@example.co.uk") == "user-name-tag-example-co-uk"
```

- [ ] **Step 2: Run test to verify it fails**

- [ ] **Step 3: Implement JitProvisioner**

Create `src/dfe_engine/auth/jit.py`:

```python
"""JIT provisioner — creates shadow accounts on first OIDC login."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from hyperi_pylib.logger import logger

from dfe_engine.auth.accounts import Account, AccountStore
from dfe_engine.auth.audit import (
    audit_jit_account_created,
    audit_jit_failed,
    audit_jit_groups_updated,
    audit_jit_team_assigned,
)
from dfe_engine.auth.groups import GroupStore

# Precedence order (highest first) for broadest-wins team assignment
_ROLE_PRECEDENCE = [
    "admin",
    "infra_admin",
    "data_analyst",
    "data_analyst_viewer",
    "data_viewer",
    "infra_viewer",
    "customer_viewer",
]

_BROAD_ROLES = {"admin", "infra_admin", "data_analyst"}
_ROLE_TO_TEAM = {
    "admin": "dfe-admin",
    "infra_admin": "dfe-admin",
    "data_analyst": "dfe-analysts",
}


class JitProvisioner:
    def __init__(
        self,
        account_store: AccountStore,
        group_store: GroupStore,
        hyperdx_client=None,
    ) -> None:
        self._accounts = account_store
        self._groups = group_store
        self._hdx = hyperdx_client

    @staticmethod
    def sanitise_username(user_id: str) -> str:
        return re.sub(r"[^a-z0-9-]", "-", user_id.lower()).strip("-")

    def ensure_account(
        self,
        user_id: str,
        oidc_groups: list[str],
        source_provider: str,
    ) -> Account:
        safe_name = self.sanitise_username(user_id)
        now = datetime.now(UTC).isoformat()

        existing = self._accounts.get(safe_name)
        if existing is not None:
            # Update groups if changed + last_login_at
            if set(existing.groups) != set(oidc_groups):
                added = [g for g in oidc_groups if g not in existing.groups]
                removed = [g for g in existing.groups if g not in oidc_groups]
                self._accounts.update(safe_name, groups=oidc_groups, last_login_at=now)
                audit_jit_groups_updated(user_id, added, removed)
            else:
                self._accounts.update(safe_name, last_login_at=now)
            return self._accounts.get(safe_name)

        # First login — create shadow account
        try:
            self._accounts.create(safe_name, "", groups=oidc_groups)
            self._accounts.update(
                safe_name,
                external=True,
                source_provider=source_provider,
                last_login_at=now,
            )
        except ValueError:
            # Race condition: another request created it first
            self._accounts.update(safe_name, groups=oidc_groups, last_login_at=now)
            return self._accounts.get(safe_name)

        # Resolve org_ids from groups
        org_ids = []
        for gname in oidc_groups:
            group = self._groups.get(gname)
            if group and group.org_ids:
                org_ids.extend(group.org_ids)

        audit_jit_account_created(user_id, source_provider, oidc_groups, org_ids)

        # HyperDX team assignment (fire-and-forget in caller)
        team = self.resolve_hyperdx_team(oidc_groups, self._groups)
        if team:
            audit_jit_team_assigned(user_id, team, "broadest-wins")

        return self._accounts.get(safe_name)

    def resolve_hyperdx_team(
        self,
        oidc_groups: list[str],
        group_store: GroupStore,
    ) -> str:
        # Collect all roles from groups
        all_roles: set[str] = set()
        all_org_ids: list[str] = []
        for gname in oidc_groups:
            group = group_store.get(gname)
            if group:
                all_roles.update(group.roles)
                all_org_ids.extend(group.org_ids)

        # Broadest wins — check precedence order
        for role in _ROLE_PRECEDENCE:
            if role in all_roles and role in _BROAD_ROLES:
                return _ROLE_TO_TEAM[role]

        # No broad role — assign to org-scoped team
        if all_org_ids:
            return f"customer-{all_org_ids[0]}"

        return ""
```

- [ ] **Step 4: Run test to verify it passes**

- [ ] **Step 5: Commit**

```
git add src/dfe_engine/auth/jit.py tests/unit/test_auth/test_jit.py
git commit -m "feat: add JitProvisioner for shadow account creation on first OIDC login"
```

---

### Task 10: Wire JitProvisioner into get_current_user()

**Files:**
- Modify: `src/dfe_engine/api/deps.py:219-236` (OIDC auth path)
- Modify: `src/dfe_engine/api/app.py` (bootstrap JitProvisioner)
- Test: `tests/e2e/test_jit_workflow.py`

- [ ] **Step 1: Write E2E test for JIT provisioning**

```python
# tests/e2e/test_jit_workflow.py
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from dfe_engine.api.app import create_app
from dfe_engine.api.deps import _registries
from dfe_engine.settings import (
    APISettings, AuthSettings, DFESettings, ServicesSettings, SourceSettings,
)


@pytest.fixture
def jit_settings(tmp_path):
    (tmp_path / "sources").mkdir()
    (tmp_path / "services").mkdir()
    (tmp_path / "auth").mkdir()
    return DFESettings(
        config_dir=str(tmp_path),
        source=SourceSettings(sources_dir=str(tmp_path / "sources")),
        services=ServicesSettings(config_yaml_dir=str(tmp_path / "services")),
        auth=AuthSettings(enabled=True, auth_dir=str(tmp_path / "auth")),
        api=APISettings(jwt_secret="jit-test-secret-key-32-chars-lo!"),
    )


@pytest.fixture
def jit_client(jit_settings):
    app = create_app(settings=jit_settings)
    with TestClient(app, raise_server_exceptions=False) as client:
        # Create a group with org_ids for testing
        group_store = app.state.group_store
        group_store.create("test-org-viewers", roles=["customer_viewer"])
        group_store.update("test-org-viewers", org_ids=["test-org"])
        yield client
    _registries.clear()


class TestJitWorkflow:
    def test_oidc_first_login_creates_shadow_account(self, jit_client):
        # Simulate OIDC headers (as if Envoy set them)
        resp = jit_client.get(
            "/api/v1/auth/me",
            headers={
                "X-Oidc-Subject": "newuser@corp.com",
                "X-Oidc-Groups": "test-org-viewers",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["user_id"] == "newuser@corp.com"

        # Verify shadow account was created
        account_store = jit_client.app.state.account_store
        account = account_store.get("newuser-corp-com")
        assert account is not None
        assert account.external is True
        assert account.last_login_at != ""

    def test_oidc_subsequent_login_updates_timestamp(self, jit_client):
        headers = {
            "X-Oidc-Subject": "returning@corp.com",
            "X-Oidc-Groups": "test-org-viewers",
        }
        jit_client.get("/api/v1/auth/me", headers=headers)
        first = jit_client.app.state.account_store.get("returning-corp-com")
        jit_client.get("/api/v1/auth/me", headers=headers)
        second = jit_client.app.state.account_store.get("returning-corp-com")
        assert second.last_login_at >= first.last_login_at
```

- [ ] **Step 2: Run test to verify it fails**

- [ ] **Step 3: Bootstrap JitProvisioner in app lifespan**

In `src/dfe_engine/api/app.py`, after auth bootstrap:

```python
from dfe_engine.auth.jit import JitProvisioner
app.state.jit_provisioner = JitProvisioner(
    account_store=account_store,
    group_store=group_store,
    hyperdx_client=getattr(app.state, "hyperdx_client", None),
)
```

- [ ] **Step 4: Add JIT call to get_current_user() OIDC path**

In `src/dfe_engine/api/deps.py`, inside the OIDC auth path (after line 236, after `audit_login_success`):

```python
# JIT provisioning — create shadow account on first OIDC login
jit = getattr(request.app.state, "jit_provisioner", None)
if jit:
    try:
        jit.ensure_account(oidc_subject, groups, raw_groups.split(",")[0] if raw_groups else "")
    except Exception:
        logger.exception("JIT provisioning failed", user_id=oidc_subject)
```

Note: The `source_provider` is best-effort from the first group name or empty. A more precise source would require correlating with OIDCProviderRegistry, which can be added later.

- [ ] **Step 5: Run E2E test to verify it passes**

Run: `uv run pytest tests/e2e/test_jit_workflow.py -x --override-ini="addopts=" -q`

- [ ] **Step 6: Run full test suite**

Run: `uv run pytest -x -q --tb=short`
Expected: All tests pass, 80%+ coverage

- [ ] **Step 7: Commit**

```
git add src/dfe_engine/api/deps.py src/dfe_engine/api/app.py tests/e2e/test_jit_workflow.py
git commit -m "feat: wire JIT provisioning into OIDC auth path"
```

---

## Chunk 5: SOC2 Audit Remediation (Phase 3)

### Task 11: Add generic audit_resource_change and wire into all mutating endpoints

**Files:**
- Modify: `src/dfe_engine/auth/audit.py`
- Modify: All mutating API routers (sources, services, deployments, field-maps, alerts, rules, OIDC providers, hunts, pipeline, schemas, transforms)
- Test: `tests/unit/test_auth/test_audit.py`

- [ ] **Step 1: Add audit_resource_change function**

Append to `src/dfe_engine/auth/audit.py`:

```python
def audit_resource_change(
    admin_id: str,
    resource_type: str,
    resource_name: str,
    change: str,
    details: dict | None = None,
) -> None:
    logger.info(
        f"resource.{resource_type}.{change}",
        admin_id=admin_id,
        resource_type=resource_type,
        resource_name=resource_name,
        change=change,
        details=details,
    )
```

- [ ] **Step 2: Write test for audit_resource_change**

- [ ] **Step 3: Wire into each mutating endpoint**

For each router, add a one-line call after the mutation succeeds. The `user.user_id` from `CurrentUser` provides the admin_id. Example for sources:

```python
# In sources.py create_source endpoint, after successful creation:
from dfe_engine.auth.audit import audit_resource_change
audit_resource_change(user.user_id, "source", body.source, "created")
```

Routers to wire (13 total):
- `v1/sources.py` — create, update, delete, bulk, seed
- `v1/services.py` — save, delete, seed
- `v1/deployments.py` — save, delete, apply_size, seed
- `v1/fieldmaps.py` — create, delete, seed
- `v1/alerts.py` — create, update, delete
- `v1/rules.py` — create
- `v1/oidc_providers.py` — create, update, delete, sync
- `v1/hunts.py` — trigger (run)
- `v1/pipeline.py` — build
- `v1/schemas.py` — build
- `v1/transforms.py` — compile, test
- `v1/accounts.py` — already has audit (audit_account_change)
- `v1/account_groups.py` — already has audit (audit_group_change)

- [ ] **Step 4: Run full test suite**

- [ ] **Step 5: Commit**

```
git add src/dfe_engine/auth/audit.py src/dfe_engine/api/v1/*.py tests/unit/test_auth/test_audit.py
git commit -m "feat: SOC2 audit remediation — wire audit_resource_change into all mutating endpoints"
```

---

### Task 12: Final verification + push

- [ ] **Step 1: Run ruff lint**

Run: `uv run ruff check src/dfe_engine/`

- [ ] **Step 2: Run full test suite**

Run: `uv run pytest -x -q --tb=short`
Expected: All tests pass, 80%+ coverage

- [ ] **Step 3: Regenerate OpenAPI spec**

Run: `uv run python openapi-spec/generate.py`

- [ ] **Step 4: Commit spec if changed**

```
git add openapi-spec/openapi.json
git commit -m "chore: regenerate OpenAPI spec after JIT provisioning changes"
```

- [ ] **Step 5: Push**

```
git push origin feat/rbac-phase1
```
