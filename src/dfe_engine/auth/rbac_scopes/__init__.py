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

# Flat catalog of every Casbin action string a role editor can assign. This was
# 26 per-category dicts in a sibling module, imported one-by-one and splatted
# together here; the split bought nothing but merge boilerplate, so it is one
# literal now. Grouped by resource (comment markers) purely for readability -
# only the key/value pairs are load-bearing.
scopes_dict = {
    # account_groups
    "group_write": "group:write",
    "group_read": "group:read",
    "group_delete": "group:delete",
    "group_add_member": "group:add_member",
    "group_remove_member": "group:remove_member",
    # accounts
    "account_write": "account:write",
    "account_read": "account:read",
    "account_delete": "account:delete",
    # Singular 'account:' namespace so the natural 'account:*' pattern covers it
    # (was the odd-one-out plural 'accounts:reset_password', which 'account:*' missed).
    "account_reset_password": "account:reset_password",
    # alerts
    "alert_read": "alert:read",
    "alert_write": "alert:write",
    "alert_delete": "alert:delete",
    # api_keys
    "api_key_read": "api_key:read",
    "api_key_write": "api_key:write",
    "api_key_delete": "api_key:delete",
    # cel
    "cel_check": "cel:check",
    # dashboard - not used in API but required for UI & HyperDX
    "dashboard_read": "dashboard:read",
    # governance (governed-ops: gitops auto-merge + Tier-2 actions/policies). Was
    # enforced by the branch-new routers but absent from this catalogue, so a role
    # editor could not offer the actions the API ships with (P2.13).
    "governance_read": "governance:read",
    "governance_write": "governance:write",
    "governance_override": "governance:override",
    # helm vars (helmvars CRUD via /helm/files, distinct from helm:compile/build)
    "helmvars_read": "helmvars:read",
    "helmvars_write": "helmvars:write",
    "helmvars_override": "helmvars:override",
    # lifecycle (service start/stop/scale via gitops)
    "lifecycle_read": "lifecycle:read",
    # deployment
    "deployment_read": "deployment:read",
    "deployment_write": "deployment:write",
    "deployment_delete": "deployment:delete",
    # discovery
    "discovery_read": "discovery:read",
    # fieldmap
    "fieldmap_read": "fieldmap:read",
    "fieldmap_write": "fieldmap:write",
    "fieldmap_delete": "fieldmap:delete",
    # hunt
    "hunt_read": "hunt:read",
    "hunt_write": "hunt:write",
    "hunt_delete": "hunt:delete",
    "hunt_execute": "hunt:execute",
    # oidc
    "oidc_read": "oidc:read",
    "oidc_write": "oidc:write",
    "oidc_delete": "oidc:delete",
    # org
    "org_read": "org:read",
    "org_write": "org:write",
    "org_delete": "org:delete",
    # pipeline
    "pipeline_read": "pipeline:read",
    "pipeline_write": "pipeline:write",
    # query
    "query_read": "query:read",
    "query_execute": "query:execute",
    # Ad-hoc arbitrary-SQL against a datasource adapter (POST /queries/raw). NOT
    # org-isolated (no org_id injection), so it is a SEPARATE, admin-level action -
    # kept off query:execute so query:execute can be an org-scoped tenant action.
    "query_raw": "query:raw",
    # repository
    "repository_read": "repository:read",
    "repository_write": "repository:write",
    # role
    "role_read": "role:read",
    "role_write": "role:write",
    "role_delete": "role:delete",
    "role_scopes": "role:scopes",
    # rules
    "rule_read": "rule:read",
    "rule_write": "rule:write",
    "rule_delete": "rule:delete",
    "rule_validate": "rule:validate",
    # sampler
    "sampler_read": "sampler:read",
    # schemas
    "schema_read": "schema:read",
    "schema_write": "schema:write",
    "schema_delete": "schema:delete",
    # service_surfaces
    "service_surface_read": "service_surface:read",
    "service_surface_write": "service_surface:write",
    # services
    "service_read": "service:read",
    "service_write": "service:write",
    "service_delete": "service:delete",
    "service_validate": "service:validate",
    # sigma
    "sigma_read": "sigma:read",
    "sigma_write": "sigma:write",
    # source
    "source_read": "source:read",
    "source_write": "source:write",
    "source_delete": "source:delete",
    # system
    "system_read": "system:read",
    # tasks
    "task_read": "task:read",
    "task_write": "task:write",
    # transform
    "transform_compile": "transform:compile",
    "transform_test": "transform:test",
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
