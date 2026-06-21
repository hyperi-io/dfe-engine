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
from dfe_engine.auth.roles import RoleConfig

account_groups_scopes = {
    "group_write": "group:write",
    "group_read": "group:read",
    "group_delete": "group:delete",
    "group_add_member": "group:add_member",
    "group_remove_member": "group:remove_member",
}

accounts_scopes = {
    "account_write": "account:write",
    "account_read": "account:read",
    "account_delete": "account:delete",
    "accounts_reset_password": "accounts:reset_password",
}

alerts_scopes = {
    "alert_read": "alert:read",
    "alert_write": "alert:write",
    "alert_delete": "alert:delete",
}

api_keys_scopes = {
    "api_key_read": "api_key:read",
    "api_key_write": "api_key:write",
    "api_key_delete": "api_key:delete",
}

cel_scopes = {
    "cel_check": "cel:check",
}

# Not used in API but required for UI & HyperDX
dashboard_scopes = {"dashboard_read": "dashboard:read"}

deployment_scopes = {
    "deployment_read": "deployment:read",
    "deployment_write": "deployment:write",
    "deployment_delete": "deployment:delete",
}

discovery_scopes = {
    "discovery_read": "discovery:read",
}

fieldmap_scopes = {
    "fieldmap_read": "fieldmap:read",
    "fieldmap_write": "fieldmap:write",
    "fieldmap_delete": "fieldmap:delete",
}

hunt_scopes = {
    "hunt_read": "hunt:read",
    "hunt_execute": "hunt:execute",
}

oidc_scopes = {
    "oidc_read": "oidc:read",
    "oidc_write": "oidc:write",
    "oidc_delete": "oidc:delete",
}

org_scopes = {
    "org_read": "org:read",
    "org_write": "org:write",
    "org_delete": "org:delete",
}

pipeline_scopes = {
    "pipeline_read": "pipeline:read",
    "pipeline_write": "pipeline:write",
}

query_scopes = {
    "query_read": "query:read",
    "query_execute": "query:execute",
}

role_scopes = {
    "role_read": "role:read",
    "role_write": "role:write",
    "role_delete": "role:delete",
    "role_scopes": "role:scopes",
}

rules_scopes = {
    "rule_read": "rule:read",
    "rule_write": "rule:write",
    "rule_delete": "rule:delete",
    "rule_validate": "rule:validate",
}

schemas_scopes = {
    "schema_read": "schema:read",
    "schema_write": "schema:write",
    "schema_delete": "schema:delete",
}

service_surfaces_scopes = {
    "service_surface_read": "service_surface:read",
    "service_surface_write": "service_surface:write",
}

services_scopes = {
    "service_read": "service:read",
    "service_write": "service:write",
    "service_delete": "service:delete",
    "service_validate": "service:validate",
}

sigma_scopes = {
    "sigma_read": "sigma:read",
    "sigma_write": "sigma:write",
}

source_scopes = {
    "source_read": "source:read",
    "source_write": "source:write",
    "source_delete": "source:delete",
}

system_scopes = {
    "system_read": "system:read",
}

tasks_scopes = {
    "task_read": "task:read",
    "task_write": "task:write",
}

transform_scopes = {
    "transform_compile": "transform:compile",
    "transform_test": "transform:test",
}

scopes_dict = {
    **account_groups_scopes,
    **accounts_scopes,
    **alerts_scopes,
    **api_keys_scopes,
    **cel_scopes,
    **dashboard_scopes,
    **deployment_scopes,
    **discovery_scopes,
    **fieldmap_scopes,
    **hunt_scopes,
    **oidc_scopes,
    **org_scopes,
    **pipeline_scopes,
    **query_scopes,
    **role_scopes,
    **rules_scopes,
    **schemas_scopes,
    **service_surfaces_scopes,
    **services_scopes,
    **sigma_scopes,
    **source_scopes,
    **system_scopes,
    **tasks_scopes,
    **transform_scopes,
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
