#  Project:      dfe-engine
#  File:         rbac_scopes.py
#  Purpose:      Casbin-style permission scopes for role configuration UI
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Catalog of RBAC permission scopes (Casbin action strings) for role editors."""

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

account_attributes_scopes = {
    "account_attributes_read": "account:attributes:read",
    "account_attributes_write": "account:attributes:write",
    "account_attributes_read_sensitive": "account:attributes:read_sensitive",
    "account_attributes_write_sensitive": "account:attributes:write_sensitive",
}

group_attributes_scopes = {
    "group_attributes_read": "group:attributes:read",
    "group_attributes_write": "group:attributes:write",
    "group_attributes_read_sensitive": "group:attributes:read_sensitive",
    "group_attributes_write_sensitive": "group:attributes:write_sensitive",
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

# ClickHouse Cloud service lifecycle (control plane; start/stop is billable).
clickhouse_cloud_scopes = {
    "clickhouse_cloud_manage": "clickhouse_cloud:manage",
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
    "hunt_write": "hunt:write",
    "hunt_delete": "hunt:delete",
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

repository_scopes = {
    "repository_read": "repository:read",
    "repository_write": "repository:write",
}

role_scopes = {
    "role_read": "role:read",
    "role_write": "role:write",
    "role_delete": "role:delete",
    "role_scopes": "role:scopes",
}

sampler_scopes = {
    "sampler_read": "sampler:read",
}

synthetic_data_scopes = {
    "synthetic_data_read": "synthetic-data:read",
    "synthetic_data_run": "synthetic-data:run",
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
    "sigma_admin": "sigma:admin",
}

source_scopes = {
    "source_read": "source:read",
    "source_write": "source:write",
    "source_delete": "source:delete",
    "source_deploy": "source:deploy",
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

governance_scopes = {
    "governance_read": "governance:read",
    "governance_write": "governance:write",
}

helmvars_scopes = {
    "helmvars_read": "helmvars:read",
    "helmvars_write": "helmvars:write",
}

library_scopes = {
    "library_read": "library:read",
    "library_write": "library:write",
}

lifecycle_scopes = {
    "lifecycle_read": "lifecycle:read",
}

config_scopes = {
    "config_write": "config:write",
}
