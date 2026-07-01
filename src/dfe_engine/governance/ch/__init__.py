#  Project:      dfe-engine
#  File:         governance/ch/__init__.py
#  Purpose:      ClickHouse org-RBAC + quota-tier machinery (config -> reconciled CH)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""ClickHouse RBAC: quota tiers + per-org row isolation.

Two reusable role axes compose every CH identity (see
docs/superpowers/specs/2026-07-01-ch-org-rbac-quota-tiers-design.md):

- a TIER role (consumption: grants + settings profile + quota), and
- an ORG role (visibility: restrictive row policy on ``_org_id``), 0 or 1.

The catalogue is gitcrud config, not code; the reconciler renders whatever tiers
+ orgs exist into idempotent ClickHouse DDL.
"""

from .models import (
    DEFAULT_SERVICE_ROLES,
    DEFAULT_TIERS,
    ChServiceRole,
    ChTier,
    GroupChBinding,
    org_policy_name,
    org_role_name,
)
from .reconciler import ChRbacReconciler, ReconcileResult, compute_drops
from .render import (
    render_group_user,
    render_materialise,
    render_org_role,
    render_service_role,
    render_service_user,
    render_tier,
)

__all__ = [
    "DEFAULT_SERVICE_ROLES",
    "DEFAULT_TIERS",
    "ChRbacReconciler",
    "ChServiceRole",
    "ChTier",
    "GroupChBinding",
    "ReconcileResult",
    "compute_drops",
    "org_policy_name",
    "org_role_name",
    "render_group_user",
    "render_materialise",
    "render_org_role",
    "render_service_role",
    "render_service_user",
    "render_tier",
]
