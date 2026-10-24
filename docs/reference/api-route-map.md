# API route map

Where each engine API operation goes in the resource hierarchy. `openapi-spec/route_map.py` generates this page from the engine's routes, `openapi-spec/route-plan.toml` and `openapi-spec/route-consumers.json`: change those and run `uv run python openapi-spec/generate.py`, never this page.

One row per operation the engine serves today. **New** is where it goes, and its operation id, `dfe` CLI command and MCP tool all come from that path. An MCP tool exists only for an operation that opts in. **Consumers** counts the files in each repo that name the old path, whatever the method (ui = dfe-ui 973f7139f030, infra = dfe-infra a235b788df2a, docker = dfe-docker 4ba5d7ab78f7, cli = dfe-engine 79d5bffd2333).

## Summary

| Status | Operations | Meaning |
|---|---|---|
| moved | 89 | A new path, method or both |
| merged into | 9 | Folded into the operation **New** names |
| unchanged | 36 | Same method and path |
| frozen | 54 | Kept as it is |
| decide | 130 | Not placed yet: see its question |
| removed | 0 | Gone, with nothing in its place |
| total | 318 | |

## Questions

| Question | Operations |
|---|---|
| [apps-depth](#apps-depth) | 21 |
| [groups-scope](#groups-scope) | 11 |
| [compute-helpers](#compute-helpers) | 10 |
| [detection-rules](#detection-rules) | 7 |
| [hunts-orgs](#hunts-orgs) | 7 |
| [services-overlap](#services-overlap) | 7 |
| [synthetic-data](#synthetic-data) | 7 |
| [queries](#queries) | 6 |
| [repository](#repository) | 6 |
| [alert-destinations](#alert-destinations) | 5 |
| [derived-schemas](#derived-schemas) | 5 |
| [fieldmap-defaults](#fieldmap-defaults) | 4 |
| [helm-vars](#helm-vars) | 4 |
| [overlays](#overlays) | 4 |
| [accounts-me](#accounts-me) | 3 |
| [clickhouse-cloud](#clickhouse-cloud) | 3 |
| [gitops-classes](#gitops-classes) | 3 |
| [samples](#samples) | 3 |
| [hyperdx](#hyperdx) | 2 |
| [lifecycle](#lifecycle) | 2 |
| [pipeline-templates](#pipeline-templates) | 2 |
| [source-schema-overlap](#source-schema-overlap) | 2 |
| [usage-scope](#usage-scope) | 2 |
| [catalogue-entry](#catalogue-entry) | 1 |
| [client-config](#client-config) | 1 |
| [defaults-apply](#defaults-apply) | 1 |
| [deployment-facts](#deployment-facts) | 1 |

## deployment

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/app-contracts/{service}` | `GET /api/v1/deployment/apps/{app}/contract` | `deployment.apps.contract.get` | `dfe deployment apps contract get` | `dfe_deployment_apps_contract_get` | moved | 0 | One contract per app image, so a singleton under the app. |
| `GET /api/v1/apps` | `GET /api/v1/deployment/apps` | `deployment.apps.list` | `dfe deployment apps list` | `dfe_deployment_apps_list` | moved | ui 4, infra 2 |  |
| `POST /api/v1/apps/{service}/instances` | `PUT /api/v1/deployment/apps/{app}/instances/{instance}` | `deployment.apps.instances.apply` | `dfe deployment apps instances apply` | `dfe_deployment_apps_instances_apply` | moved | ui 3 | One PUT creates (`If-None-Match: *`) and replaces (`If-Match`). |
| `DELETE /api/v1/apps/{service}/{instance}` | `DELETE /api/v1/deployment/apps/{app}/instances/{instance}` | `deployment.apps.instances.delete` | `dfe deployment apps instances delete` | `dfe_deployment_apps_instances_delete` | moved | ui 2 |  |
| `GET /api/v1/apps/{service}/{instance}` | `GET /api/v1/deployment/apps/{app}/instances/{instance}` | `deployment.apps.instances.get` | `dfe deployment apps instances get` | `dfe_deployment_apps_instances_get` | moved | ui 2 |  |
| `GET /api/v1/backing-services` | `GET /api/v1/deployment/backing-services` | `deployment.backing-services.list` | `dfe deployment backing-services list` | `dfe_deployment_backing_services_list` | moved | ui 2, infra 1 |  |
| `GET /api/v1/backing-services/{service}` | `GET /api/v1/deployment/backing-services/{service}` | `deployment.backing-services.get` | `dfe deployment backing-services get` | `dfe_deployment_backing_services_get` | moved | 0 |  |
| `GET /api/v1/deployment/admin-links` | `GET /api/v1/deployment/admin-links` | `deployment.admin-links.list` | `dfe deployment admin-links list` | `dfe_deployment_admin_links_list` | unchanged | ui 3, infra 1 |  |
| `GET /api/v1/kafka/topics` | `GET /api/v1/deployment/topics` | `deployment.topics.list` | `dfe deployment topics list` | `dfe_deployment_topics_list` | moved | infra 1 |  |
| `POST /api/v1/kafka/topics/ensure` | `POST /api/v1/deployment/topics/ensure` | `deployment.topics.ensure` | `dfe deployment topics ensure` | `dfe_deployment_topics_ensure` | moved | 0 |  |
| `POST /api/v1/kafka/topics/remove` | `POST /api/v1/deployment/topics/remove` | `deployment.topics.remove` | `dfe deployment topics remove` | `dfe_deployment_topics_remove` | moved | 0 |  |
| `POST /api/v1/kafka/topics/update` | `POST /api/v1/deployment/topics/update` | `deployment.topics.update` | `dfe deployment topics update` | `dfe_deployment_topics_update` | moved | 0 |  |

## governance

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/gitops/auto-merge` | `GET /api/v1/governance/settings/{setting}` | `governance.settings.get` | `dfe governance settings get` | `dfe_governance_settings_get` | moved | ui 3 | Auto-merge is one of the governance settings. |
| `PUT /api/v1/gitops/auto-merge` | `PUT /api/v1/governance/settings/{setting}` | `governance.settings.apply` | `dfe governance settings apply` | `dfe_governance_settings_apply` | moved | ui 3 | Auto-merge is one of the governance settings. |
| `GET /api/v1/gitops/log` | `GET /api/v1/governance/changes` | `governance.changes.list` | `dfe governance changes list` | `dfe_governance_changes_list` | moved | ui 2 | The gitops log is the change history. |
| `GET /api/v1/governance/actions` | `GET /api/v1/governance/actions` | `governance.actions.list` | `dfe governance actions list` | `dfe_governance_actions_list` | unchanged | ui 2 |  |
| `GET /api/v1/governance/actions/{name}` | `GET /api/v1/governance/actions/{action}` | `governance.actions.get` | `dfe governance actions get` | `dfe_governance_actions_get` | moved | ui 2 | Same URL: the path parameter is renamed. |
| `POST /api/v1/governance/actions/{name}/invoke` | `POST /api/v1/governance/actions/{action}/invoke` | `governance.actions.invoke` | `dfe governance actions invoke` | `dfe_governance_actions_invoke` | moved | ui 3 | Same URL: the path parameter is renamed. |
| `POST /api/v1/governance/admin/actions` | `PUT /api/v1/governance/actions/{action}` | `governance.actions.apply` | `dfe governance actions apply` | `dfe_governance_actions_apply` | moved | ui 3 | One PUT creates (`If-None-Match: *`) and replaces (`If-Match`). |
| `POST /api/v1/governance/admin/actions/validate` | `PUT /api/v1/governance/actions/{action}` | `governance.actions.apply` | `dfe governance actions apply` | `dfe_governance_actions_apply` | merged into | 0 | Validation is the apply with `dry_run=true`. |
| `DELETE /api/v1/governance/admin/actions/{name}` | `DELETE /api/v1/governance/actions/{action}` | `governance.actions.delete` | `dfe governance actions delete` | `dfe_governance_actions_delete` | moved | ui 3 |  |
| `POST /api/v1/governance/admin/policies` | `PUT /api/v1/governance/policies/{policy}` | `governance.policies.apply` | `dfe governance policies apply` | `dfe_governance_policies_apply` | moved | ui 2 | One PUT creates (`If-None-Match: *`) and replaces (`If-Match`). |
| `DELETE /api/v1/governance/admin/policies/{name}` | `DELETE /api/v1/governance/policies/{policy}` | `governance.policies.delete` | `dfe governance policies delete` | `dfe_governance_policies_delete` | moved | ui 2 |  |
| `POST /api/v1/governance/ch-rbac/reconcile` | `POST /api/v1/governance/ch-rbac/reconcile` | `governance.ch-rbac.reconcile` | `dfe governance ch-rbac reconcile` | `dfe_governance_ch_rbac_reconcile` | unchanged | ui 2 |  |
| `GET /api/v1/governance/policies` | `GET /api/v1/governance/policies` | `governance.policies.list` | `dfe governance policies list` | `dfe_governance_policies_list` | unchanged | ui 2 |  |
| `GET /api/v1/governance/policies/{name}` | `GET /api/v1/governance/policies/{policy}` | `governance.policies.get` | `dfe governance policies get` | `dfe_governance_policies_get` | moved | ui 2 | Same URL: the path parameter is renamed. |
| `GET /api/v1/library` | `GET /api/v1/governance/library` | `governance.library.list` | `dfe governance library list` | `dfe_governance_library_list` | moved | ui 3, infra 1 |  |
| `POST /api/v1/library` | `PUT /api/v1/governance/library/{item}` | `governance.library.apply` | `dfe governance library apply` | `dfe_governance_library_apply` | moved | ui 3, infra 1 | One PUT creates (`If-None-Match: *`) and replaces (`If-Match`). |
| `GET /api/v1/library/kinds` | `GET /api/v1/governance/library/kinds` | `governance.library.kinds` | `dfe governance library kinds` | `dfe_governance_library_kinds` | moved | ui 2 |  |
| `DELETE /api/v1/library/{artifact}` | `DELETE /api/v1/governance/library/{item}` | `governance.library.delete` | `dfe governance library delete` | `dfe_governance_library_delete` | moved | ui 2 |  |
| `GET /api/v1/library/{artifact}` | `GET /api/v1/governance/library/{item}` | `governance.library.get` | `dfe governance library get` | `dfe_governance_library_get` | moved | ui 2 |  |
| `PATCH /api/v1/library/{artifact}` | `PUT /api/v1/governance/library/{item}` | `governance.library.apply` | `dfe governance library apply` | `dfe_governance_library_apply` | merged into | ui 2 | PATCH folds into the apply, which takes the whole desired state. |
| `POST /api/v1/library/{artifact}/rollback` | `POST /api/v1/governance/library/{item}/rollback` | `governance.library.rollback` | `dfe governance library rollback` | `dfe_governance_library_rollback` | moved | ui 2 |  |
| `PUT /api/v1/library/{artifact}/state` | `PUT /api/v1/governance/library/{item}/state` | `governance.library.state.apply` | `dfe governance library state apply` | `dfe_governance_library_state_apply` | moved | ui 2 |  |
| `DELETE /api/v1/library/{artifact}/tags/{tag}` | `DELETE /api/v1/governance/library/{item}/tags/{tag}` | `governance.library.tags.delete` | `dfe governance library tags delete` | `dfe_governance_library_tags_delete` | moved | ui 2 |  |
| `PUT /api/v1/library/{artifact}/tags/{tag}` | `PUT /api/v1/governance/library/{item}/tags/{tag}` | `governance.library.tags.apply` | `dfe governance library tags apply` | `dfe_governance_library_tags_apply` | moved | ui 2 |  |
| `GET /api/v1/library/{artifact}/usage` | `GET /api/v1/governance/library/{item}/usage` | `governance.library.usage.get` | `dfe governance library usage get` | `dfe_governance_library_usage_get` | moved | ui 2 |  |
| `GET /api/v1/library/{artifact}/versions` | `GET /api/v1/governance/library/{item}/versions` | `governance.library.versions.list` | `dfe governance library versions list` | `dfe_governance_library_versions_list` | moved | ui 2 |  |
| `POST /api/v1/library/{artifact}/versions` | `POST /api/v1/governance/library/{item}/versions` | `governance.library.versions.create` | `dfe governance library versions create` | `dfe_governance_library_versions_create` | moved | ui 2 |  |
| `GET /api/v1/library/{artifact}/versions/{version}` | `GET /api/v1/governance/library/{item}/versions/{version}` | `governance.library.versions.get` | `dfe governance library versions get` | `dfe_governance_library_versions_get` | moved | ui 2 |  |

## identity

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/auth/accounts` | `GET /api/v1/identity/accounts` | `identity.accounts.list` | `dfe identity accounts list` | `dfe_identity_accounts_list` | moved | ui 2, cli 1 |  |
| `POST /api/v1/auth/accounts` | `PUT /api/v1/identity/accounts/{account}` | `identity.accounts.apply` | `dfe identity accounts apply` | `dfe_identity_accounts_apply` | merged into | ui 2, cli 1 | One PUT creates (`If-None-Match: *`) and replaces (`If-Match`). |
| `DELETE /api/v1/auth/accounts/{username}` | `DELETE /api/v1/identity/accounts/{account}` | `identity.accounts.delete` | `dfe identity accounts delete` | `dfe_identity_accounts_delete` | moved | ui 2 |  |
| `GET /api/v1/auth/accounts/{username}` | `GET /api/v1/identity/accounts/{account}` | `identity.accounts.get` | `dfe identity accounts get` | `dfe_identity_accounts_get` | moved | ui 2 |  |
| `PUT /api/v1/auth/accounts/{username}` | `PUT /api/v1/identity/accounts/{account}` | `identity.accounts.apply` | `dfe identity accounts apply` | `dfe_identity_accounts_apply` | moved | ui 2 |  |
| `GET /api/v1/auth/accounts/{username}/attributes` | `GET /api/v1/identity/accounts/{account}/attributes` | `identity.accounts.attributes.get` | `dfe identity accounts attributes get` | `dfe_identity_accounts_attributes_get` | moved | 0 |  |
| `PUT /api/v1/auth/accounts/{username}/attributes` | `PUT /api/v1/identity/accounts/{account}/attributes` | `identity.accounts.attributes.apply` | `dfe identity accounts attributes apply` | `dfe_identity_accounts_attributes_apply` | moved | 0 |  |
| `GET /api/v1/auth/accounts/{username}/git-status` | `GET /api/v1/identity/accounts/{account}/git-status` | `identity.accounts.git-status` | `dfe identity accounts git-status` | `dfe_identity_accounts_git_status` | moved | 0 |  |
| `POST /api/v1/auth/accounts/{username}/reset-password` | `POST /api/v1/identity/accounts/{account}/reset-password` | `identity.accounts.reset-password` | `dfe identity accounts reset-password` | `dfe_identity_accounts_reset_password` | moved | ui 2 |  |
| `POST /api/v1/auth/accounts/{username}/rotate-password` | `POST /api/v1/identity/accounts/{account}/rotate-password` | `identity.accounts.rotate-password` | `dfe identity accounts rotate-password` | `dfe_identity_accounts_rotate_password` | moved | 0 |  |
| `GET /api/v1/auth/accounts/{username}/sensitive-attributes` | `GET /api/v1/identity/accounts/{account}/sensitive-attributes` | `identity.accounts.sensitive-attributes.get` | `dfe identity accounts sensitive-attributes get` | `dfe_identity_accounts_sensitive_attributes_get` | moved | 0 |  |
| `PUT /api/v1/auth/accounts/{username}/sensitive-attributes` | `PUT /api/v1/identity/accounts/{account}/sensitive-attributes` | `identity.accounts.sensitive-attributes.apply` | `dfe identity accounts sensitive-attributes apply` | `dfe_identity_accounts_sensitive_attributes_apply` | moved | 0 |  |
| `GET /api/v1/auth/api-keys` | `GET /api/v1/identity/api-keys` | `identity.api-keys.list` | `dfe identity api-keys list` | `dfe_identity_api_keys_list` | moved | ui 2 |  |
| `POST /api/v1/auth/api-keys` | `POST /api/v1/identity/api-keys` | `identity.api-keys.create` | `dfe identity api-keys create` | `dfe_identity_api_keys_create` | moved | ui 2 |  |
| `DELETE /api/v1/auth/api-keys/{short_token}` | `DELETE /api/v1/identity/api-keys/{key}` | `identity.api-keys.delete` | `dfe identity api-keys delete` | `dfe_identity_api_keys_delete` | moved | ui 2 |  |
| `GET /api/v1/auth/oidc-providers` | `GET /api/v1/identity/oidc-providers` | `identity.oidc-providers.list` | `dfe identity oidc-providers list` | `dfe_identity_oidc_providers_list` | moved | ui 2 |  |
| `POST /api/v1/auth/oidc-providers` | `PUT /api/v1/identity/oidc-providers/{provider}` | `identity.oidc-providers.apply` | `dfe identity oidc-providers apply` | `dfe_identity_oidc_providers_apply` | merged into | ui 2 | One PUT creates (`If-None-Match: *`) and replaces (`If-Match`). |
| `DELETE /api/v1/auth/oidc-providers/{name}` | `DELETE /api/v1/identity/oidc-providers/{provider}` | `identity.oidc-providers.delete` | `dfe identity oidc-providers delete` | `dfe_identity_oidc_providers_delete` | moved | ui 2 |  |
| `GET /api/v1/auth/oidc-providers/{name}` | `GET /api/v1/identity/oidc-providers/{provider}` | `identity.oidc-providers.get` | `dfe identity oidc-providers get` | `dfe_identity_oidc_providers_get` | moved | ui 2 |  |
| `PUT /api/v1/auth/oidc-providers/{name}` | `PUT /api/v1/identity/oidc-providers/{provider}` | `identity.oidc-providers.apply` | `dfe identity oidc-providers apply` | `dfe_identity_oidc_providers_apply` | moved | ui 2 |  |
| `POST /api/v1/auth/oidc-providers/{name}/sync` | `POST /api/v1/identity/oidc-providers/{provider}/sync` | `identity.oidc-providers.sync` | `dfe identity oidc-providers sync` | `dfe_identity_oidc_providers_sync` | moved | ui 2 |  |
| `GET /api/v1/auth/oidc-providers/{name}/test` | `GET /api/v1/identity/oidc-providers/{provider}/test` | `identity.oidc-providers.test` | `dfe identity oidc-providers test` | `dfe_identity_oidc_providers_test` | moved | ui 2 |  |
| `GET /api/v1/auth/oidc-providers/{name}/verify-login` | `GET /api/v1/identity/oidc-providers/{provider}/verify-login` | `identity.oidc-providers.verify-login` | `dfe identity oidc-providers verify-login` | `dfe_identity_oidc_providers_verify_login` | moved | ui 2 |  |
| `GET /api/v1/auth/roles` | `GET /api/v1/identity/roles` | `identity.roles.list` | `dfe identity roles list` | `dfe_identity_roles_list` | moved | ui 2 |  |
| `POST /api/v1/auth/roles` | `PUT /api/v1/identity/roles/{role}` | `identity.roles.apply` | `dfe identity roles apply` | `dfe_identity_roles_apply` | merged into | ui 2 | One PUT creates (`If-None-Match: *`) and replaces (`If-Match`). |
| `GET /api/v1/auth/roles/scopes` | `GET /api/v1/identity/roles/scopes` | `identity.roles.scopes` | `dfe identity roles scopes` | `dfe_identity_roles_scopes` | moved | ui 2 |  |
| `DELETE /api/v1/auth/roles/{name}` | `DELETE /api/v1/identity/roles/{role}` | `identity.roles.delete` | `dfe identity roles delete` | `dfe_identity_roles_delete` | moved | ui 2 |  |
| `GET /api/v1/auth/roles/{name}` | `GET /api/v1/identity/roles/{role}` | `identity.roles.get` | `dfe identity roles get` | `dfe_identity_roles_get` | moved | ui 2 |  |
| `PUT /api/v1/auth/roles/{name}` | `PUT /api/v1/identity/roles/{role}` | `identity.roles.apply` | `dfe identity roles apply` | `dfe_identity_roles_apply` | moved | ui 2 |  |

## ingest

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/ingest/compat` | `GET /api/v1/ingest/compat` | `ingest.compat.get` | `dfe ingest compat get` | `dfe_ingest_compat_get` | unchanged | 0 |  |
| `PUT /api/v1/ingest/compat` | `PUT /api/v1/ingest/compat` | `ingest.compat.apply` | `dfe ingest compat apply` | `dfe_ingest_compat_apply` | unchanged | 0 |  |
| `GET /api/v1/ingest/compat-profiles` | `GET /api/v1/ingest/compat-profiles` | `ingest.compat-profiles.list` | `dfe ingest compat-profiles list` | `dfe_ingest_compat_profiles_list` | unchanged | 0 |  |
| `GET /api/v1/ingest/compat-profiles/{profile}` | `GET /api/v1/ingest/compat-profiles/{profile}` | `ingest.compat-profiles.get` | `dfe ingest compat-profiles get` | `dfe_ingest_compat_profiles_get` | unchanged | 0 |  |
| `GET /api/v1/ingest/rules` | `GET /api/v1/ingest/rules` | `ingest.rules.list` | `dfe ingest rules list` | `dfe_ingest_rules_list` | unchanged | 0 |  |
| `POST /api/v1/ingest/rules/order` | `POST /api/v1/ingest/rules/order` | `ingest.rules.order` | `dfe ingest rules order` | `dfe_ingest_rules_order` | unchanged | 0 |  |

## orgs

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/orgs` | `GET /api/v1/orgs` | `orgs.list` | `dfe orgs list` | `dfe_orgs_list` | unchanged | ui 5, infra 1, cli 1 |  |
| `POST /api/v1/orgs` | `PUT /api/v1/orgs/{org}` | `orgs.apply` | `dfe orgs apply` | `dfe_orgs_apply` | merged into | ui 5, infra 1, cli 1 | One PUT creates (`If-None-Match: *`) and replaces (`If-Match`). |
| `GET /api/v1/orgs/available-ids` | `GET /api/v1/orgs/available-ids` | `orgs.available-ids` | `dfe orgs available-ids` | `dfe_orgs_available_ids` | unchanged | 0 |  |
| `DELETE /api/v1/orgs/{name}` | `DELETE /api/v1/orgs/{org}` | `orgs.delete` | `dfe orgs delete` | `dfe_orgs_delete` | moved | ui 3 | Same URL: the path parameter is renamed. |
| `GET /api/v1/orgs/{name}` | `GET /api/v1/orgs/{org}` | `orgs.get` | `dfe orgs get` | `dfe_orgs_get` | moved | ui 3 | Same URL: the path parameter is renamed. |
| `PUT /api/v1/orgs/{name}` | `PUT /api/v1/orgs/{org}` | `orgs.apply` | `dfe orgs apply` | `dfe_orgs_apply` | moved | ui 3 | Same URL: the path parameter is renamed. |
| `GET /api/v1/orgs/{org}/ingest-credentials` | `GET /api/v1/orgs/{org}/ingest-credentials` | `orgs.ingest-credentials.list` | `dfe orgs ingest-credentials list` | `dfe_orgs_ingest_credentials_list` | unchanged | 0 |  |
| `DELETE /api/v1/orgs/{org}/ingest-credentials/{credential}` | `DELETE /api/v1/orgs/{org}/ingest-credentials/{credential}` | `orgs.ingest-credentials.delete` | `dfe orgs ingest-credentials delete` | `dfe_orgs_ingest_credentials_delete` | unchanged | 0 |  |
| `GET /api/v1/orgs/{org}/ingest-credentials/{credential}` | `GET /api/v1/orgs/{org}/ingest-credentials/{credential}` | `orgs.ingest-credentials.get` | `dfe orgs ingest-credentials get` | `dfe_orgs_ingest_credentials_get` | unchanged | 0 |  |
| `PUT /api/v1/orgs/{org}/ingest-credentials/{credential}` | `PUT /api/v1/orgs/{org}/ingest-credentials/{credential}` | `orgs.ingest-credentials.apply` | `dfe orgs ingest-credentials apply` | `dfe_orgs_ingest_credentials_apply` | unchanged | 0 |  |
| `POST /api/v1/orgs/{org}/ingest-credentials/{credential}/rotate` | `POST /api/v1/orgs/{org}/ingest-credentials/{credential}/rotate` | `orgs.ingest-credentials.rotate` | `dfe orgs ingest-credentials rotate` | `dfe_orgs_ingest_credentials_rotate` | unchanged | 0 |  |

## schemas

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/schemas` | `GET /api/v1/schemas` | `schemas.list` | `dfe schemas list` | `dfe_schemas_list` | unchanged | ui 2, infra 1 |  |
| `DELETE /api/v1/schemas/definitions/{schema_path}` | `DELETE /api/v1/schemas/{schema}` | `schemas.delete` | `dfe schemas delete` | `dfe_schemas_delete` | moved | ui 5 |  |
| `PATCH /api/v1/schemas/definitions/{schema_path}` | `PUT /api/v1/schemas/{schema}` | `schemas.apply` | `dfe schemas apply` | `dfe_schemas_apply` | merged into | ui 5 | PATCH folds into the apply, which takes the whole desired state. |
| `POST /api/v1/schemas/definitions/{schema_path}` | `PUT /api/v1/schemas/{schema}` | `schemas.apply` | `dfe schemas apply` | `dfe_schemas_apply` | moved | ui 5 | One PUT creates (`If-None-Match: *`) and replaces (`If-Match`). |
| `GET /api/v1/schemas/definitions/{schema_path}/export` | `GET /api/v1/schemas/{schema}/export` | `schemas.export` | `dfe schemas export` | `dfe_schemas_export` | moved | 0 |  |
| `POST /api/v1/schemas/definitions/{schema_path}/versions` | `POST /api/v1/schemas/{schema}/versions` | `schemas.versions.create` | `dfe schemas versions create` | `dfe_schemas_versions_create` | moved | ui 2 |  |
| `GET /api/v1/schemas/definitions/{schema_path}/versions/columns` | `GET /api/v1/schemas/{schema}/versions/columns` | `schemas.versions.columns` | `dfe schemas versions columns` | `dfe_schemas_versions_columns` | moved | ui 3 |  |
| `DELETE /api/v1/schemas/definitions/{schema_path}/versions/{version}` | `DELETE /api/v1/schemas/{schema}/versions/{version}` | `schemas.versions.delete` | `dfe schemas versions delete` | `dfe_schemas_versions_delete` | moved | ui 2 |  |
| `POST /api/v1/schemas/elastic-converter` | `POST /api/v1/schemas/elastic-converter` | `schemas.elastic-converter` | `dfe schemas elastic-converter` | `dfe_schemas_elastic_converter` | unchanged | ui 2 |  |
| `POST /api/v1/schemas/import` | `POST /api/v1/schemas/import` | `schemas.import` | `dfe schemas import` | `dfe_schemas_import` | unchanged | 0 |  |

## sources

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `DELETE /api/v1/field-maps/{standard}/{source}` | `DELETE /api/v1/sources/{source}/field-maps/{field_map}` | `sources.field-maps.delete` | `dfe sources field-maps delete` | `dfe_sources_field_maps_delete` | moved | ui 2 |  |
| `GET /api/v1/field-maps/{standard}/{source}` | `GET /api/v1/sources/{source}/field-maps/{field_map}` | `sources.field-maps.get` | `dfe sources field-maps get` | `dfe_sources_field_maps_get` | moved | ui 2 |  |
| `GET /api/v1/schemas/{source_name}/json-paths` | `GET /api/v1/sources/{source}/json-paths` | `sources.json-paths` | `dfe sources json-paths` | `dfe_sources_json_paths` | moved | ui 3 |  |
| `POST /api/v1/schemas/{source_name}/promote-field` | `POST /api/v1/sources/{source}/promote-field` | `sources.promote-field` | `dfe sources promote-field` | `dfe_sources_promote_field` | moved | ui 3 |  |
| `GET /api/v1/schemas/{source_name}/sample-rows` | `GET /api/v1/sources/{source}/sample-rows` | `sources.sample-rows` | `dfe sources sample-rows` | `dfe_sources_sample_rows` | moved | ui 2 |  |
| `GET /api/v1/sources` | `GET /api/v1/sources` | `sources.list` | `dfe sources list` | `dfe_sources_list` | unchanged | ui 5, infra 4, docker 1 |  |
| `POST /api/v1/sources` | `PUT /api/v1/sources/{source}` | `sources.apply` | `dfe sources apply` | `dfe_sources_apply` | merged into | ui 5, infra 4, docker 1 | One PUT creates (`If-None-Match: *`) and replaces (`If-Match`). |
| `POST /api/v1/sources/bulk` | `POST /api/v1/sources/bulk` | `sources.bulk` | `dfe sources bulk` | `dfe_sources_bulk` | unchanged | ui 2 |  |
| `GET /api/v1/sources/catalogue` | `GET /api/v1/sources/catalogue` | `sources.catalogue` | `dfe sources catalogue` | `dfe_sources_catalogue` | unchanged | ui 2 |  |
| `GET /api/v1/sources/engines` | `GET /api/v1/sources/engines` | `sources.engines` | `dfe sources engines` | `dfe_sources_engines` | unchanged | ui 2 |  |
| `POST /api/v1/sources/import` | `POST /api/v1/sources/import` | `sources.import` | `dfe sources import` | `dfe_sources_import` | unchanged | 0 |  |
| `POST /api/v1/sources/reconcile-apps` | `POST /api/v1/sources/reconcile-apps` | `sources.reconcile-apps` | `dfe sources reconcile-apps` | `dfe_sources_reconcile_apps` | unchanged | 0 |  |
| `POST /api/v1/sources/seed` | `POST /api/v1/sources/seed` | `sources.seed` | `dfe sources seed` | `dfe_sources_seed` | unchanged | ui 2 |  |
| `DELETE /api/v1/sources/{name}` | `DELETE /api/v1/sources/{source}` | `sources.delete` | `dfe sources delete` | `dfe_sources_delete` | moved | ui 8, infra 2, docker 1 | Same URL: the path parameter is renamed. |
| `GET /api/v1/sources/{name}` | `GET /api/v1/sources/{source}` | `sources.get` | `dfe sources get` | `dfe_sources_get` | moved | ui 8, infra 2, docker 1 | Same URL: the path parameter is renamed. |
| `PATCH /api/v1/sources/{name}` | `PUT /api/v1/sources/{source}` | `sources.apply` | `dfe sources apply` | `dfe_sources_apply` | merged into | ui 8, infra 2, docker 1 | PATCH folds into the apply, which takes the whole desired state. |
| `PUT /api/v1/sources/{name}` | `PUT /api/v1/sources/{source}` | `sources.apply` | `dfe sources apply` | `dfe_sources_apply` | moved | ui 8, infra 2, docker 1 | Same URL: the path parameter is renamed. |
| `POST /api/v1/sources/{name}/build` | `POST /api/v1/sources/{source}/build` | `sources.build` | `dfe sources build` | `dfe_sources_build` | moved | ui 2 | Same URL: the path parameter is renamed. |
| `GET /api/v1/sources/{name}/columns` | `GET /api/v1/sources/{source}/columns` | `sources.columns.list` | `dfe sources columns list` | `dfe_sources_columns_list` | moved | ui 2 | Same URL: the path parameter is renamed. |
| `POST /api/v1/sources/{name}/deploy` | `POST /api/v1/sources/{source}/deploy` | `sources.deploy` | `dfe sources deploy` | `dfe_sources_deploy` | moved | ui 4, infra 1 | Same URL: the path parameter is renamed. |
| `GET /api/v1/sources/{name}/export` | `GET /api/v1/sources/{source}/export` | `sources.export` | `dfe sources export` | `dfe_sources_export` | moved | 0 | Same URL: the path parameter is renamed. |
| `GET /api/v1/sources/{name}/flow` | `GET /api/v1/sources/{source}/flow` | `sources.flow` | `dfe sources flow` | `dfe_sources_flow` | moved | ui 2 | Same URL: the path parameter is renamed. |
| `POST /api/v1/sources/{name}/plan` | `POST /api/v1/sources/{source}/plan` | `sources.plan` | `dfe sources plan` | `dfe_sources_plan` | moved | ui 2 | Same URL: the path parameter is renamed. |
| `GET /api/v1/sources/{name}/signals` | `GET /api/v1/sources/{source}/signals` | `sources.signals` | `dfe sources signals` | `dfe_sources_signals` | moved | 0 | Same URL: the path parameter is renamed. |
| `GET /api/v1/sources/{name}/versions/{version}` | `GET /api/v1/sources/{source}/versions/{version}` | `sources.versions.get` | `dfe sources versions get` | `dfe_sources_versions_get` | moved | ui 2 | Same URL: the path parameter is renamed. |
| `POST /api/v1/sources/{source}/sample` | `POST /api/v1/sources/{source}/sample` | `sources.sample` | `dfe sources sample` | `dfe_sources_sample` | unchanged | 0 |  |

## system

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/system/defaults` | `GET /api/v1/system/defaults` | `system.defaults.get` | `dfe system defaults get` | `dfe_system_defaults_get` | unchanged | ui 2 |  |
| `PATCH /api/v1/system/defaults` | `PUT /api/v1/system/defaults` | `system.defaults.apply` | `dfe system defaults apply` | `dfe_system_defaults_apply` | moved | ui 2 | PATCH folds into the apply, which takes the whole desired state. |
| `GET /api/v1/system/defaults/drift` | `GET /api/v1/system/defaults/drift` | `system.defaults.drift` | `dfe system defaults drift` | `dfe_system_defaults_drift` | unchanged | ui 2 |  |
| `GET /api/v1/system/retention` | `GET /api/v1/system/retention` | `system.retention.get` | `dfe system retention get` | `dfe_system_retention_get` | unchanged | ui 2 |  |
| `PUT /api/v1/system/retention` | `PUT /api/v1/system/retention` | `system.retention.apply` | `dfe system retention apply` | `dfe_system_retention_apply` | unchanged | ui 2 |  |
| `GET /api/v1/system/schema` | `GET /api/v1/system/schema` | `system.schema` | `dfe system schema` | `dfe_system_schema` | unchanged | infra 98, docker 3 |  |
| `GET /api/v1/system/settings` | `GET /api/v1/system/settings` | `system.settings` | `dfe system settings` | `dfe_system_settings` | unchanged | ui 2 |  |
| `GET /api/v1/system/status` | `GET /api/v1/system/status` | `system.status` | `dfe system status` | `dfe_system_status` | unchanged | 0 |  |
| `GET /api/v1/system/version` | `GET /api/v1/system/version` | `system.version` | `dfe system version` | `dfe_system_version` | unchanged | ui 3, infra 1 |  |
| `GET /api/v1/tasks` | `GET /api/v1/system/operations` | `system.operations.list` | `dfe system operations list` | `dfe_system_operations_list` | moved | infra 1 | Background tasks are operations (AIP-151). |
| `GET /api/v1/tasks/{task_id}` | `GET /api/v1/system/operations/{operation}` | `system.operations.get` | `dfe system operations get` | `dfe_system_operations_get` | moved | 0 | Background tasks are operations (AIP-151). |
| `POST /api/v1/tasks/{task_id}/cancel` | `POST /api/v1/system/operations/{operation}/cancel` | `system.operations.cancel` | `dfe system operations cancel` | `dfe_system_operations_cancel` | moved | 0 | Background tasks are operations (AIP-151). |

## Frozen: protocol

Protocol surface: token issue, OIDC or SCIM (RFC 7644) fixes its shape.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `POST /api/v1/auth/login` | `POST /api/v1/auth/login` | `auth.login` | `dfe auth login` | - | frozen | ui 6, infra 1, docker 1, cli 1 |  |
| `POST /api/v1/auth/logout` | `POST /api/v1/auth/logout` | `auth.logout` | `dfe auth logout` | - | frozen | ui 3 |  |
| `GET /api/v1/auth/me` | `GET /api/v1/auth/me` | `auth.me.list` | `dfe auth me list` | - | frozen | ui 7, cli 1 |  |
| `GET /api/v1/auth/oidc/{provider}/callback` | `GET /api/v1/auth/oidc/{provider}/callback` | `auth.oidc.callback.list` | `dfe auth oidc callback list` | - | frozen | ui 2, infra 3 |  |
| `GET /api/v1/auth/oidc/{provider}/login` | `GET /api/v1/auth/oidc/{provider}/login` | `auth.oidc.login.list` | `dfe auth oidc login list` | - | frozen | ui 3, infra 1 |  |
| `GET /api/v1/auth/permissions` | `GET /api/v1/auth/permissions` | `auth.permissions.list` | `dfe auth permissions list` | - | frozen | ui 2 |  |
| `POST /api/v1/auth/refresh` | `POST /api/v1/auth/refresh` | `auth.refresh` | `dfe auth refresh` | - | frozen | ui 5 |  |
| `GET /api/v1/auth/setup-status` | `GET /api/v1/auth/setup-status` | `auth.setup-status.list` | `dfe auth setup-status list` | - | frozen | ui 4, infra 5, docker 3 |  |
| `POST /api/v1/auth/setup/retire-admin` | `POST /api/v1/auth/setup/retire-admin` | `auth.setup.retire-admin` | `dfe auth setup retire-admin` | - | frozen | docker 1 |  |
| `GET /api/v1/scim/v2/Groups` | `GET /api/v1/scim/v2/Groups` | `scim.v2.groups.list` | `dfe scim v2 groups list` | - | frozen | 0 |  |
| `POST /api/v1/scim/v2/Groups` | `POST /api/v1/scim/v2/Groups` | `scim.v2.groups.create` | `dfe scim v2 groups create` | - | frozen | 0 |  |
| `DELETE /api/v1/scim/v2/Groups/{group_id}` | `DELETE /api/v1/scim/v2/Groups/{group_id}` | `scim.v2.groups.delete` | `dfe scim v2 groups delete` | - | frozen | 0 |  |
| `GET /api/v1/scim/v2/Groups/{group_id}` | `GET /api/v1/scim/v2/Groups/{group_id}` | `scim.v2.groups.get` | `dfe scim v2 groups get` | - | frozen | 0 |  |
| `PATCH /api/v1/scim/v2/Groups/{group_id}` | `PATCH /api/v1/scim/v2/Groups/{group_id}` | `scim.v2.groups.update` | `dfe scim v2 groups update` | - | frozen | 0 |  |
| `PUT /api/v1/scim/v2/Groups/{group_id}` | `PUT /api/v1/scim/v2/Groups/{group_id}` | `scim.v2.groups.apply` | `dfe scim v2 groups apply` | - | frozen | 0 |  |
| `GET /api/v1/scim/v2/ResourceTypes` | `GET /api/v1/scim/v2/ResourceTypes` | `scim.v2.resource-types.list` | `dfe scim v2 resource-types list` | - | frozen | 0 |  |
| `GET /api/v1/scim/v2/Schemas` | `GET /api/v1/scim/v2/Schemas` | `scim.v2.schemas.list` | `dfe scim v2 schemas list` | - | frozen | 0 |  |
| `GET /api/v1/scim/v2/ServiceProviderConfig` | `GET /api/v1/scim/v2/ServiceProviderConfig` | `scim.v2.service-provider-config.list` | `dfe scim v2 service-provider-config list` | - | frozen | 0 |  |
| `GET /api/v1/scim/v2/Users` | `GET /api/v1/scim/v2/Users` | `scim.v2.users.list` | `dfe scim v2 users list` | - | frozen | infra 1 |  |
| `POST /api/v1/scim/v2/Users` | `POST /api/v1/scim/v2/Users` | `scim.v2.users.create` | `dfe scim v2 users create` | - | frozen | infra 1 |  |
| `DELETE /api/v1/scim/v2/Users/{user_id}` | `DELETE /api/v1/scim/v2/Users/{user_id}` | `scim.v2.users.delete` | `dfe scim v2 users delete` | - | frozen | 0 |  |
| `GET /api/v1/scim/v2/Users/{user_id}` | `GET /api/v1/scim/v2/Users/{user_id}` | `scim.v2.users.get` | `dfe scim v2 users get` | - | frozen | 0 |  |
| `PATCH /api/v1/scim/v2/Users/{user_id}` | `PATCH /api/v1/scim/v2/Users/{user_id}` | `scim.v2.users.update` | `dfe scim v2 users update` | - | frozen | 0 |  |
| `PUT /api/v1/scim/v2/Users/{user_id}` | `PUT /api/v1/scim/v2/Users/{user_id}` | `scim.v2.users.apply` | `dfe scim v2 users apply` | - | frozen | 0 |  |

## Frozen: sigma

Inert for 2.2.1 and hidden from the CLI and MCP. Placed when sigma returns.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/sigma/bindings` | `GET /api/v1/sigma/bindings` | `sigma.bindings.list` | - | - | frozen | 0 |  |
| `DELETE /api/v1/sigma/bindings/{rule_id}` | `DELETE /api/v1/sigma/bindings/{rule_id}` | `sigma.bindings.delete` | - | - | frozen | 0 |  |
| `GET /api/v1/sigma/bindings/{rule_id}` | `GET /api/v1/sigma/bindings/{rule_id}` | `sigma.bindings.get` | - | - | frozen | 0 |  |
| `GET /api/v1/sigma/catalogue` | `GET /api/v1/sigma/catalogue` | `sigma.catalogue.list` | - | - | frozen | 0 |  |
| `DELETE /api/v1/sigma/catalogue/{rule_id}` | `DELETE /api/v1/sigma/catalogue/{rule_id}` | `sigma.catalogue.delete` | - | - | frozen | 0 |  |
| `GET /api/v1/sigma/catalogue/{rule_id}` | `GET /api/v1/sigma/catalogue/{rule_id}` | `sigma.catalogue.get` | - | - | frozen | 0 |  |
| `PUT /api/v1/sigma/catalogue/{rule_id}` | `PUT /api/v1/sigma/catalogue/{rule_id}` | `sigma.catalogue.apply` | - | - | frozen | 0 |  |
| `POST /api/v1/sigma/catalogue/{rule_id}/adopt` | `POST /api/v1/sigma/catalogue/{rule_id}/adopt` | `sigma.catalogue.adopt` | - | - | frozen | 0 |  |
| `POST /api/v1/sigma/catalogue/{rule_id}/deselect` | `POST /api/v1/sigma/catalogue/{rule_id}/deselect` | `sigma.catalogue.deselect` | - | - | frozen | 0 |  |
| `POST /api/v1/sigma/catalogue/{rule_id}/select` | `POST /api/v1/sigma/catalogue/{rule_id}/select` | `sigma.catalogue.select` | - | - | frozen | 0 |  |
| `GET /api/v1/sigma/logsource` | `GET /api/v1/sigma/logsource` | `sigma.logsource.list` | - | - | frozen | 0 |  |
| `GET /api/v1/sigma/mappings/{source_name}` | `GET /api/v1/sigma/mappings/{source_name}` | `sigma.mappings.get` | - | - | frozen | 0 |  |
| `POST /api/v1/sigma/propagate` | `POST /api/v1/sigma/propagate` | `sigma.propagate` | - | - | frozen | 0 |  |
| `GET /api/v1/sigma/propagations/{task_id}` | `GET /api/v1/sigma/propagations/{task_id}` | `sigma.propagations.get` | - | - | frozen | 0 |  |
| `GET /api/v1/sigma/providers` | `GET /api/v1/sigma/providers` | `sigma.providers.list` | - | - | frozen | 0 |  |
| `POST /api/v1/sigma/providers` | `POST /api/v1/sigma/providers` | `sigma.providers.create` | - | - | frozen | 0 |  |
| `DELETE /api/v1/sigma/providers/{name}` | `DELETE /api/v1/sigma/providers/{name}` | `sigma.providers.delete` | - | - | frozen | 0 |  |
| `GET /api/v1/sigma/providers/{name}` | `GET /api/v1/sigma/providers/{name}` | `sigma.providers.get` | - | - | frozen | 0 |  |
| `PUT /api/v1/sigma/providers/{name}` | `PUT /api/v1/sigma/providers/{name}` | `sigma.providers.apply` | - | - | frozen | 0 |  |
| `POST /api/v1/sigma/providers/{name}/disable` | `POST /api/v1/sigma/providers/{name}/disable` | `sigma.providers.disable` | - | - | frozen | 0 |  |
| `POST /api/v1/sigma/providers/{name}/enable` | `POST /api/v1/sigma/providers/{name}/enable` | `sigma.providers.enable` | - | - | frozen | 0 |  |
| `POST /api/v1/sigma/providers/{name}/sync` | `POST /api/v1/sigma/providers/{name}/sync` | `sigma.providers.sync` | - | - | frozen | 0 |  |
| `GET /api/v1/sigma/selected` | `GET /api/v1/sigma/selected` | `sigma.selected.list` | - | - | frozen | 0 |  |
| `GET /api/v1/sigma/syncs/{task_id}` | `GET /api/v1/sigma/syncs/{task_id}` | `sigma.syncs.get` | - | - | frozen | 0 |  |
| `GET /api/v1/sigma/views` | `GET /api/v1/sigma/views` | `sigma.views.list` | - | - | frozen | 0 |  |
| `POST /api/v1/sigma/views` | `POST /api/v1/sigma/views` | `sigma.views.create` | - | - | frozen | 0 |  |
| `DELETE /api/v1/sigma/views/{source_name}` | `DELETE /api/v1/sigma/views/{source_name}` | `sigma.views.delete` | - | - | frozen | 0 |  |
| `GET /api/v1/sigma/views/{source_name}` | `GET /api/v1/sigma/views/{source_name}` | `sigma.views.get` | - | - | frozen | 0 |  |
| `POST /api/v1/sigma/views/{source_name}` | `POST /api/v1/sigma/views/{source_name}` | `sigma.views.post` | - | - | frozen | 0 |  |
| `PUT /api/v1/sigma/views/{source_name}` | `PUT /api/v1/sigma/views/{source_name}` | `sigma.views.apply` | - | - | frozen | 0 |  |

## To decide

### apps-depth

An instance's children sit 4 levels deep under the `deployment` root: drop `deployment` from the apps path, or raise the depth limit to 4.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `DELETE /api/v1/apps/{service}/{instance}/config` | - | - | - | - | decide | ui 3 |  |
| `GET /api/v1/apps/{service}/{instance}/config` | - | - | - | - | decide | ui 3 |  |
| `PUT /api/v1/apps/{service}/{instance}/config` | - | - | - | - | decide | ui 3 |  |
| `GET /api/v1/apps/{service}/{instance}/files/{set_name}` | - | - | - | - | decide | ui 3 |  |
| `POST /api/v1/apps/{service}/{instance}/files/{set_name}/copy` | - | - | - | - | decide | ui 2 |  |
| `POST /api/v1/apps/{service}/{instance}/files/{set_name}/dry-run` | - | - | - | - | decide | ui 2 |  |
| `POST /api/v1/apps/{service}/{instance}/files/{set_name}/link` | - | - | - | - | decide | ui 2 |  |
| `GET /api/v1/apps/{service}/{instance}/files/{set_name}/links` | - | - | - | - | decide | ui 2 |  |
| `POST /api/v1/apps/{service}/{instance}/files/{set_name}/relink` | - | - | - | - | decide | ui 2 |  |
| `DELETE /api/v1/apps/{service}/{instance}/files/{set_name}/{filename}` | - | - | - | - | decide | ui 3 |  |
| `GET /api/v1/apps/{service}/{instance}/files/{set_name}/{filename}` | - | - | - | - | decide | ui 3 |  |
| `PUT /api/v1/apps/{service}/{instance}/files/{set_name}/{filename}` | - | - | - | - | decide | ui 3 |  |
| `GET /api/v1/apps/{service}/{instance}/history` | - | - | - | - | decide | ui 2 |  |
| `GET /api/v1/apps/{service}/{instance}/metrics` | - | - | - | - | decide | ui 2 |  |
| `GET /api/v1/apps/{service}/{instance}/metrics/series` | - | - | - | - | decide | ui 2 |  |
| `GET /api/v1/apps/{service}/{instance}/routing` | - | - | - | - | decide | ui 2 |  |
| `POST /api/v1/apps/{service}/{instance}/routing/sync` | - | - | - | - | decide | ui 2 |  |
| `GET /api/v1/apps/{service}/{instance}/scaling` | - | - | - | - | decide | ui 5 |  |
| `PUT /api/v1/apps/{service}/{instance}/scaling` | - | - | - | - | decide | ui 5 |  |
| `GET /api/v1/apps/{service}/{instance}/status` | - | - | - | - | decide | ui 2 |  |
| `GET /api/v1/apps/{service}/{instance}/values` | - | - | - | - | decide | 0 |  |

### groups-scope

A group is system- or org-scoped, so `orgs/{org}/groups` cannot own a system group: one `identity/groups/{group}` collection with the scope as a field, or split between the two.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/auth/groups` | - | - | - | - | decide | ui 2 |  |
| `POST /api/v1/auth/groups` | - | - | - | - | decide | ui 2 |  |
| `DELETE /api/v1/auth/groups/{name}` | - | - | - | - | decide | ui 2 |  |
| `GET /api/v1/auth/groups/{name}` | - | - | - | - | decide | ui 2 |  |
| `PUT /api/v1/auth/groups/{name}` | - | - | - | - | decide | ui 2 |  |
| `GET /api/v1/auth/groups/{name}/attributes` | - | - | - | - | decide | 0 |  |
| `PUT /api/v1/auth/groups/{name}/attributes` | - | - | - | - | decide | 0 |  |
| `POST /api/v1/auth/groups/{name}/members` | - | - | - | - | decide | ui 2 |  |
| `DELETE /api/v1/auth/groups/{name}/members/{username}` | - | - | - | - | decide | ui 2 |  |
| `GET /api/v1/auth/groups/{name}/sensitive-attributes` | - | - | - | - | decide | 0 |  |
| `PUT /api/v1/auth/groups/{name}/sensitive-attributes` | - | - | - | - | decide | 0 |  |

### compute-helpers

A compute-only helper that owns no resource: an action on the resource it serves, or a shared root for helpers.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `POST /api/v1/authoring/ai/create` | - | - | - | - | decide | 0 |  |
| `POST /api/v1/authoring/ai/generate-vrl` | - | - | - | - | decide | 0 |  |
| `POST /api/v1/authoring/ai/review` | - | - | - | - | decide | 0 |  |
| `POST /api/v1/authoring/ai/suggest-schema` | - | - | - | - | decide | 0 |  |
| `POST /api/v1/authoring/from-hyperdx` | - | - | - | - | decide | 0 |  |
| `POST /api/v1/authoring/scaffold` | - | - | - | - | decide | 0 |  |
| `POST /api/v1/cel/check` | - | - | - | - | decide | 0 |  |
| `POST /api/v1/cel/check-batch` | - | - | - | - | decide | 0 |  |
| `POST /api/v1/transforms/compile` | - | - | - | - | decide | ui 2 |  |
| `POST /api/v1/transforms/test` | - | - | - | - | decide | ui 2 |  |

### detection-rules

Detection rules (hunt SQL and CEL): `rules` is kept for routing rules under `sources` and `ingest`, so these need a place of their own.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/rules` | - | - | - | - | decide | ui 3, infra 1 |  |
| `POST /api/v1/rules` | - | - | - | - | decide | ui 3, infra 1 |  |
| `POST /api/v1/rules/from-hyperdx` | - | - | - | - | decide | 0 |  |
| `POST /api/v1/rules/validate` | - | - | - | - | decide | ui 2 |  |
| `DELETE /api/v1/rules/{name}` | - | - | - | - | decide | ui 3 |  |
| `GET /api/v1/rules/{name}` | - | - | - | - | decide | ui 3 |  |
| `PUT /api/v1/rules/{name}` | - | - | - | - | decide | ui 3 |  |

### hunts-orgs

A hunt can target several customer orgs, so `orgs/{org}/hunts` cannot own every hunt.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/hunts` | - | - | - | - | decide | ui 4, infra 1 |  |
| `POST /api/v1/hunts` | - | - | - | - | decide | ui 4, infra 1 |  |
| `GET /api/v1/hunts/status` | - | - | - | - | decide | ui 2 |  |
| `DELETE /api/v1/hunts/{name}` | - | - | - | - | decide | ui 4 |  |
| `GET /api/v1/hunts/{name}` | - | - | - | - | decide | ui 4 |  |
| `PUT /api/v1/hunts/{name}` | - | - | - | - | decide | ui 4 |  |
| `POST /api/v1/hunts/{name}/run` | - | - | - | - | decide | ui 2 |  |

### services-overlap

ServiceConfigRegistry overlaps the apps router's instance config: merge into `deployment/apps/{app}/instances/{instance}`, or retire.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/services` | - | - | - | - | decide | infra 1 |  |
| `POST /api/v1/services/seed` | - | - | - | - | decide | 0 |  |
| `DELETE /api/v1/services/{service}/{instance}` | - | - | - | - | decide | 0 |  |
| `GET /api/v1/services/{service}/{instance}` | - | - | - | - | decide | 0 |  |
| `PUT /api/v1/services/{service}/{instance}` | - | - | - | - | decide | 0 |  |
| `GET /api/v1/services/{service}/{instance}/history` | - | - | - | - | decide | 0 |  |
| `POST /api/v1/services/{service}/{instance}/validate` | - | - | - | - | decide | 0 |  |

### synthetic-data

No root holds synthetic data.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `POST /api/v1/synthetic-data/generate` | - | - | - | - | decide | 0 |  |
| `POST /api/v1/synthetic-data/lookalike` | - | - | - | - | decide | 0 |  |
| `GET /api/v1/synthetic-data/packs` | - | - | - | - | decide | 0 |  |
| `POST /api/v1/synthetic-data/stream` | - | - | - | - | decide | 0 |  |
| `GET /api/v1/synthetic-data/streams` | - | - | - | - | decide | 0 |  |
| `DELETE /api/v1/synthetic-data/streams/{task_id}` | - | - | - | - | decide | 0 |  |
| `GET /api/v1/synthetic-data/streams/{task_id}` | - | - | - | - | decide | 0 |  |

### queries

No root holds queries: parameterised views, raw SQL and the cost leaderboard.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/queries/cost-leaderboard` | - | - | - | - | decide | 0 |  |
| `POST /api/v1/queries/raw` | - | - | - | - | decide | 0 |  |
| `GET /api/v1/queries/views` | - | - | - | - | decide | 0 |  |
| `GET /api/v1/queries/views/namespaces` | - | - | - | - | decide | 0 |  |
| `GET /api/v1/queries/views/{label}` | - | - | - | - | decide | 0 |  |
| `POST /api/v1/queries/views/{label}/execute` | - | - | - | - | decide | 0 |  |

### repository

Console preferences and small objects scoped system, org or user: no root holds them.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/repository/objects/{scope}/{scope_id}/{namespace}` | - | - | - | - | decide | ui 2 |  |
| `DELETE /api/v1/repository/objects/{scope}/{scope_id}/{namespace}/{key}` | - | - | - | - | decide | ui 2 |  |
| `GET /api/v1/repository/objects/{scope}/{scope_id}/{namespace}/{key}` | - | - | - | - | decide | ui 2 |  |
| `PUT /api/v1/repository/objects/{scope}/{scope_id}/{namespace}/{key}` | - | - | - | - | decide | ui 2 |  |
| `GET /api/v1/repository/preferences` | - | - | - | - | decide | ui 2 |  |
| `PATCH /api/v1/repository/preferences` | - | - | - | - | decide | ui 2 |  |

### alert-destinations

Alert destinations are deployment-wide, optionally bound to a hunt: under `orgs/{org}` with alerts, or deployment-wide.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/alerts/destinations` | - | - | - | - | decide | ui 2 |  |
| `POST /api/v1/alerts/destinations` | - | - | - | - | decide | ui 2 |  |
| `DELETE /api/v1/alerts/destinations/{name}` | - | - | - | - | decide | ui 2 |  |
| `GET /api/v1/alerts/destinations/{name}` | - | - | - | - | decide | ui 2 |  |
| `PUT /api/v1/alerts/destinations/{name}` | - | - | - | - | decide | ui 2 |  |

### derived-schemas

A derived schema's base is not in its path today, and nesting under `schemas/{schema}/derived` needs it there.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/schemas/definitions/derived` | - | - | - | - | decide | 0 |  |
| `DELETE /api/v1/schemas/definitions/derived/{schema_path}` | - | - | - | - | decide | 0 |  |
| `GET /api/v1/schemas/definitions/derived/{schema_path}` | - | - | - | - | decide | 0 |  |
| `POST /api/v1/schemas/definitions/derived/{schema_path}` | - | - | - | - | decide | 0 |  |
| `PUT /api/v1/schemas/definitions/derived/{schema_path}` | - | - | - | - | decide | 0 |  |

### fieldmap-defaults

Default field maps belong to no source: an `ingest/field-maps/{field_map}` collection, or another root.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/field-maps` | - | - | - | - | decide | ui 2, infra 1 |  |
| `POST /api/v1/field-maps` | - | - | - | - | decide | ui 2, infra 1 |  |
| `POST /api/v1/field-maps/seed` | - | - | - | - | decide | ui 2 |  |
| `GET /api/v1/field-maps/{standard}` | - | - | - | - | decide | ui 2 |  |

### helm-vars

Generic overlay-var CRUD: under `governance/settings`, under `deployment`, or retired once apps and backing services cover it.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/helm/files` | - | - | - | - | decide | ui 2 |  |
| `GET /api/v1/helm/files/{name}/vars` | - | - | - | - | decide | ui 2 |  |
| `DELETE /api/v1/helm/files/{name}/vars/{path}` | - | - | - | - | decide | ui 2 |  |
| `PUT /api/v1/helm/files/{name}/vars/{path}` | - | - | - | - | decide | ui 2 |  |

### overlays

Backing-service overlay files: under `deployment/backing-services/{service}`, which needs each overlay's service, or a collection of their own.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/backing-services/overlays` | - | - | - | - | decide | 0 |  |
| `GET /api/v1/backing-services/overlays/{name}/vars` | - | - | - | - | decide | 0 |  |
| `DELETE /api/v1/backing-services/overlays/{name}/vars/{path}` | - | - | - | - | decide | ui 2 |  |
| `PUT /api/v1/backing-services/overlays/{name}/vars/{path}` | - | - | - | - | decide | ui 2 |  |

### accounts-me

The caller's own account: an `identity/accounts/me` alias, or an `identity/me` singleton beside the frozen `auth/me`.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/auth/accounts/me` | - | - | - | - | decide | ui 2 |  |
| `PUT /api/v1/auth/accounts/me` | - | - | - | - | decide | ui 2 |  |
| `POST /api/v1/auth/accounts/reset-password` | - | - | - | - | decide | ui 2 |  |

### clickhouse-cloud

ClickHouse Cloud status, start and stop: actions on `deployment/backing-services/{service}`, or a singleton of their own.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/system/clickhouse-cloud` | - | - | - | - | decide | ui 2 |  |
| `POST /api/v1/system/clickhouse-cloud/start` | - | - | - | - | decide | ui 2 |  |
| `POST /api/v1/system/clickhouse-cloud/stop` | - | - | - | - | decide | ui 2 |  |

### gitops-classes

The resource-class enumeration generated clients build their selects from: no root holds it.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/gitops/classes` | - | - | - | - | decide | 0 |  |
| `GET /api/v1/gitops/classes/{cls}/resources` | - | - | - | - | decide | 0 |  |
| `GET /api/v1/gitops/classes/{cls}/resources/{name}/vars` | - | - | - | - | decide | 0 |  |

### samples

Sample tasks: fold into `system/operations/{operation}` with the result inline, or keep a collection of their own.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `POST /api/v1/sample` | - | - | - | - | decide | infra 1 |  |
| `GET /api/v1/samples` | - | - | - | - | decide | infra 1 |  |
| `GET /api/v1/samples/{task_id}` | - | - | - | - | decide | 0 |  |

### hyperdx

The HyperDX fork's reads for the caller's org: under `orgs/{org}`, or a root of their own.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/hyperdx/connection` | - | - | - | - | decide | 0 |  |
| `GET /api/v1/hyperdx/sources` | - | - | - | - | decide | ui 1 |  |

### lifecycle

Lifecycle state covers apps and backing services: an action on each, or a collection of its own.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/lifecycle` | - | - | - | - | decide | ui 2, infra 1 |  |
| `POST /api/v1/lifecycle/{name}` | - | - | - | - | decide | ui 2 |  |

### pipeline-templates

Whether these Vector templates are what the `pipelines/{pipeline}` root holds.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `POST /api/v1/pipeline/build` | - | - | - | - | decide | 0 |  |
| `GET /api/v1/pipeline/templates` | - | - | - | - | decide | 0 |  |

### source-schema-overlap

Overlaps the sources router's own route for the same source: merge into it, or keep both.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `POST /api/v1/schemas/{source_name}/build` | - | - | - | - | decide | 0 |  |
| `GET /api/v1/schemas/{source_name}/columns` | - | - | - | - | decide | 0 |  |

### usage-scope

Usage reads the whole deployment or one org, and `orgs/{org}/usage` covers only the second.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/usage` | - | - | - | - | decide | 0 |  |
| `GET /api/v1/usage/overview` | - | - | - | - | decide | 0 |  |

### catalogue-entry

Creating from a catalogue entry: a collection action `sources/from-catalogue` with the entry in the body, or catalogue entries as a resource of their own.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `POST /api/v1/sources/from-catalogue/{entry}` | - | - | - | - | decide | ui 2 |  |

### client-config

The console's runtime bootstrap: `system/client-config`, or where the console first reads it.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/config/client` | - | - | - | - | decide | ui 2, infra 1 |  |

### defaults-apply

The action's name is the apply verb, which `PUT /system/defaults` takes.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `POST /api/v1/system/defaults/apply` | - | - | - | - | decide | ui 2 |  |

### deployment-facts

What this deployment runs: the `deployment` singleton's own read, kept for the Big Dial, or a read beside it.

| Old | New | Operation id | CLI | MCP tool | Status | Consumers | Note |
|---|---|---|---|---|---|---|---|
| `GET /api/v1/system/deployment` | - | - | - | - | decide | infra 2 |  |
