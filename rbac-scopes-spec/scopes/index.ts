// Auto-generated from dfe-engine scope_constants.py. Do not edit.

const account_groups_scopes = {
  group_write: "group:write",
  group_read: "group:read",
  group_delete: "group:delete",
  group_add_member: "group:add_member",
  group_remove_member: "group:remove_member",
} as const;

const accounts_scopes = {
  account_write: "account:write",
  account_read: "account:read",
  account_delete: "account:delete",
  accounts_reset_password: "accounts:reset_password",
} as const;

const alerts_scopes = {
  alert_read: "alert:read",
  alert_write: "alert:write",
  alert_delete: "alert:delete",
} as const;

const api_keys_scopes = {
  api_key_read: "api_key:read",
  api_key_write: "api_key:write",
  api_key_delete: "api_key:delete",
} as const;

const cel_scopes = {
  cel_check: "cel:check",
} as const;

const clickhouse_cloud_scopes = {
  clickhouse_cloud_manage: "clickhouse_cloud:manage",
} as const;

const dashboard_scopes = {
  dashboard_read: "dashboard:read",
} as const;

const deployment_scopes = {
  deployment_read: "deployment:read",
  deployment_write: "deployment:write",
  deployment_delete: "deployment:delete",
} as const;

const discovery_scopes = {
  discovery_read: "discovery:read",
} as const;

const fieldmap_scopes = {
  fieldmap_read: "fieldmap:read",
  fieldmap_write: "fieldmap:write",
  fieldmap_delete: "fieldmap:delete",
} as const;

const hunt_scopes = {
  hunt_read: "hunt:read",
  hunt_write: "hunt:write",
  hunt_delete: "hunt:delete",
  hunt_execute: "hunt:execute",
} as const;

const oidc_scopes = {
  oidc_read: "oidc:read",
  oidc_write: "oidc:write",
  oidc_delete: "oidc:delete",
} as const;

const org_scopes = {
  org_read: "org:read",
  org_write: "org:write",
  org_delete: "org:delete",
} as const;

const pipeline_scopes = {
  pipeline_read: "pipeline:read",
  pipeline_write: "pipeline:write",
} as const;

const query_scopes = {
  query_read: "query:read",
  query_execute: "query:execute",
} as const;

const repository_scopes = {
  repository_read: "repository:read",
  repository_write: "repository:write",
} as const;

const role_scopes = {
  role_read: "role:read",
  role_write: "role:write",
  role_delete: "role:delete",
  role_scopes: "role:scopes",
} as const;

const sampler_scopes = {
  sampler_read: "sampler:read",
} as const;

const rules_scopes = {
  rule_read: "rule:read",
  rule_write: "rule:write",
  rule_delete: "rule:delete",
  rule_validate: "rule:validate",
} as const;

const schemas_scopes = {
  schema_read: "schema:read",
  schema_write: "schema:write",
  schema_delete: "schema:delete",
} as const;

const service_surfaces_scopes = {
  service_surface_read: "service_surface:read",
  service_surface_write: "service_surface:write",
} as const;

const services_scopes = {
  service_read: "service:read",
  service_write: "service:write",
  service_delete: "service:delete",
  service_validate: "service:validate",
} as const;

const sigma_scopes = {
  sigma_read: "sigma:read",
  sigma_write: "sigma:write",
} as const;

const source_scopes = {
  source_read: "source:read",
  source_write: "source:write",
  source_delete: "source:delete",
  source_deploy: "source:deploy",
} as const;

const system_scopes = {
  system_read: "system:read",
} as const;

const tasks_scopes = {
  task_read: "task:read",
  task_write: "task:write",
} as const;

const transform_scopes = {
  transform_compile: "transform:compile",
  transform_test: "transform:test",
} as const;

export const scopes = {
  ...account_groups_scopes,
  ...accounts_scopes,
  ...alerts_scopes,
  ...api_keys_scopes,
  ...cel_scopes,
  ...clickhouse_cloud_scopes,
  ...dashboard_scopes,
  ...deployment_scopes,
  ...discovery_scopes,
  ...fieldmap_scopes,
  ...hunt_scopes,
  ...oidc_scopes,
  ...org_scopes,
  ...pipeline_scopes,
  ...query_scopes,
  ...repository_scopes,
  ...role_scopes,
  ...sampler_scopes,
  ...rules_scopes,
  ...schemas_scopes,
  ...service_surfaces_scopes,
  ...services_scopes,
  ...sigma_scopes,
  ...source_scopes,
  ...system_scopes,
  ...tasks_scopes,
  ...transform_scopes,
} as const;

export type RbacScope = (typeof scopes)[keyof typeof scopes];
