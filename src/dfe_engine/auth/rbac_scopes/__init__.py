#  Project:      dfe-engine
#  File:         rbac_scopes.py
#  Purpose:      Casbin-style permission scopes for role configuration UI
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Catalog of RBAC permission scopes (Casbin action strings) for role editors."""

from __future__ import annotations

from dfe_engine.auth.engine import API_ENFORCED_ACTIONS, ARGO_ACTION_PREFIX, ENGINE_ACTIONS
from dfe_engine.auth.rbac_scopes.scope_constants import (
    account_attributes_scopes,
    account_groups_scopes,
    accounts_scopes,
    alerts_scopes,
    api_keys_scopes,
    cel_scopes,
    clickhouse_cloud_scopes,
    config_scopes,
    dashboard_scopes,
    deployment_scopes,
    discovery_scopes,
    fieldmap_scopes,
    governance_scopes,
    group_attributes_scopes,
    helmvars_scopes,
    hunt_scopes,
    library_scopes,
    lifecycle_scopes,
    oidc_scopes,
    org_scopes,
    pipeline_scopes,
    query_scopes,
    repository_scopes,
    role_scopes,
    rules_scopes,
    sampler_scopes,
    schemas_scopes,
    service_surfaces_scopes,
    services_scopes,
    sigma_scopes,
    source_scopes,
    synthetic_data_scopes,
    system_scopes,
    tasks_scopes,
    transform_scopes,
)
from dfe_engine.auth.roles import RoleConfig

scopes_dict = {
    **account_groups_scopes,
    **accounts_scopes,
    **account_attributes_scopes,
    **group_attributes_scopes,
    **alerts_scopes,
    **api_keys_scopes,
    **cel_scopes,
    **clickhouse_cloud_scopes,
    **dashboard_scopes,
    **synthetic_data_scopes,
    **deployment_scopes,
    **discovery_scopes,
    **fieldmap_scopes,
    **hunt_scopes,
    **oidc_scopes,
    **org_scopes,
    **pipeline_scopes,
    **query_scopes,
    **repository_scopes,
    **role_scopes,
    **rules_scopes,
    **sampler_scopes,
    **schemas_scopes,
    **service_surfaces_scopes,
    **services_scopes,
    **sigma_scopes,
    **source_scopes,
    **system_scopes,
    **tasks_scopes,
    **transform_scopes,
    **governance_scopes,
    **helmvars_scopes,
    **library_scopes,
    **lifecycle_scopes,
    **config_scopes,
}


def casbin_scopes_for_role_configuration() -> list[str]:
    """Return sorted permission patterns assignable in ``roles.yaml``.

    Includes built-in role patterns, engine-documented actions, and every
    action enforced by the HTTP API via ``require_action()``.
    """
    patterns: set[str] = set(ENGINE_ACTIONS)
    patterns.update(API_ENFORCED_ACTIONS)
    patterns.update(scopes_dict.values())
    for role in RoleConfig.load_builtin().roles.values():
        patterns.update(role.permissions)
    return sorted(patterns)


def casbin_scope_catalog() -> dict[str, object]:
    """Metadata for UIs building role permission pickers."""
    return {
        "scopes": casbin_scopes_for_role_configuration(),
        "wildcard": True,
        "argo_namespace_prefix": ARGO_ACTION_PREFIX,
        "notes": (
            "Permission patterns use colon-separated segments. "
            "A trailing * matches any suffix (e.g. config:*). "
            "Mid-segment * matches one segment (e.g. service:*:config:read). "
            "Bare * grants all actions. "
            f"Argo CD actions use the {ARGO_ACTION_PREFIX!r} prefix."
        ),
    }
