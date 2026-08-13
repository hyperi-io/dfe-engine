#  Project:      dfe-engine
#  File:         governance/ch/__init__.py
#  Purpose:      ClickHouse tenant-RBAC + quota-tier machinery (config -> reconciled CH)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""ClickHouse RBAC: quota tiers + pinned-setting tenant isolation.

Every CH identity composes from (see
docs/superpowers/specs/2026-07-01-ch-org-rbac-quota-tiers-design.md):

- a TIER role (consumption: grants + settings profile + quota), and
- the tenant axis (visibility): ONE shared role + one restrictive row policy per
  ``_org_id`` table, whose predicate reads the caller's ``SQL_current_tenant_id``
  setting. An org-tied user carries that setting PINNED READONLY, so a
  ``SETTINGS`` override in attacker-authored query text is a hard 452. Platform
  identities simply do not hold the tenant role and read unrestricted.

The catalogue is gitcrud config, not code; the reconciler renders whatever tiers
+ orgs exist into idempotent ClickHouse DDL.
"""

from .bindings import derive_group_bindings
from .bootstrap import ch_admin_client, reconcile_from_stores
from .models import (
    DEFAULT_SERVICE_ROLES,
    DEFAULT_TIERS,
    TENANT_ROLE,
    TENANT_SETTING,
    ChServiceRole,
    ChTier,
    GroupChBinding,
    org_user_name,
    tenant_policy_name,
)
from .reconciler import ChRbacReconciler, ReconcileResult, compute_drops, reconcile_ch_rbac
from .render import (
    render_materialise,
    render_pinned_user,
    render_service_role,
    render_service_user,
    render_tenant_axis,
    render_tier,
)

__all__ = [
    "DEFAULT_SERVICE_ROLES",
    "DEFAULT_TIERS",
    "TENANT_ROLE",
    "TENANT_SETTING",
    "ChRbacReconciler",
    "ChServiceRole",
    "ChTier",
    "GroupChBinding",
    "ReconcileResult",
    "ch_admin_client",
    "compute_drops",
    "derive_group_bindings",
    "org_user_name",
    "reconcile_ch_rbac",
    "reconcile_from_stores",
    "render_materialise",
    "render_pinned_user",
    "render_service_role",
    "render_service_user",
    "render_tenant_axis",
    "render_tier",
    "tenant_policy_name",
]
