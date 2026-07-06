#  Project:      dfe-engine
#  File:         governance/ch/__init__.py
#  Purpose:      ClickHouse org-RBAC + quota-tier machinery (config -> reconciled CH)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""ClickHouse RBAC: quota tiers + custom-settings tenant isolation.

The consumption axis is unchanged (quota TIER roles + fixed SERVICE roles; see
docs/superpowers/specs/2026-07-01-ch-org-rbac-quota-tiers-design.md). The tenant
axis is the PRODUCTION-STANDARD custom-settings model (docs/RBAC.md section 5):

- a SMALL FIXED set of CH users by PRIVILEGE (``FIXED_USERS`` + ``dfe_admin``),
  and
- ONE ``SQL_current_tenant_id``-driven RESTRICTIVE row policy per ``_org_id``
  table (``render_tenant_policies``), targeting only ``dfe_tenant_reader``.

Adding an org is zero DDL; the engine injects the org id into the per-query
setting. The catalogue is gitcrud config, not code; the reconciler renders it into
idempotent ClickHouse DDL.
"""

from .models import (
    ADMIN_USER,
    ANALYST_RO_USER,
    ANALYST_USER,
    DEFAULT_SERVICE_ROLES,
    DEFAULT_TIERS,
    FIXED_USERS,
    TENANT_POLICY_NAME,
    TENANT_READER_USER,
    TENANT_SETTING,
    ChFixedUser,
    ChServiceRole,
    ChTier,
)
from .reconciler import (
    ChRbacReconciler,
    ReconcileResult,
    compute_drops,
    load_catalogue_from_gitcrud,
    reconcile_ch_rbac,
)
from .render import (
    render_fixed_users,
    render_materialise,
    render_service_role,
    render_service_user,
    render_tenant_policies,
    render_tier,
)

__all__ = [
    "ADMIN_USER",
    "ANALYST_RO_USER",
    "ANALYST_USER",
    "DEFAULT_SERVICE_ROLES",
    "DEFAULT_TIERS",
    "FIXED_USERS",
    "TENANT_POLICY_NAME",
    "TENANT_READER_USER",
    "TENANT_SETTING",
    "ChFixedUser",
    "ChRbacReconciler",
    "ChServiceRole",
    "ChTier",
    "ReconcileResult",
    "compute_drops",
    "load_catalogue_from_gitcrud",
    "reconcile_ch_rbac",
    "render_fixed_users",
    "render_materialise",
    "render_service_role",
    "render_service_user",
    "render_tenant_policies",
    "render_tier",
]
