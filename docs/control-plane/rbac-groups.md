# RBAC - IdP group sync and links

This page is section 4.4 of [rbac.md](rbac.md), kept under that number so section references resolve.

## 4.4 Group Sync Process

A sync run for an api-mode provider lists every remote group and updates, creates or skips each one (`src/dfe_engine/auth/oidc/sync.py`).

```mermaid
sequenceDiagram
    accTitle: L4 Detail -- OIDC group sync
    accDescr: The sync reads the provider from the registry, lists every group through the provider's adapter, and reads the stored groups. Each remote group then updates, creates or is skipped, and the sync writes the run's time and status back to the registry.
    participant Sync as sync_provider
    participant Reg as Provider registry
    participant Adapter as Provider adapter
    participant GS as GroupStore
    Sync->>Reg: Get the provider
    Reg-->>Sync: Provider config
    Sync->>Adapter: list_all_groups
    Adapter-->>Sync: Every group
    Sync->>GS: list
    GS-->>Sync: Stored groups and their links
    loop Each remote group
        Sync->>GS: Update, create or skip, as Figure 2 decides
    end
    Sync->>Reg: Update last_sync_at and last_sync_status
```

*Figure 1. L4 Detail -- OIDC group sync. Solid arrows are requests, dashed arrows are responses.* Up: [rbac.md](rbac.md).

A remote group linked through this provider updates its group, and a clash with another provider or a taken name is skipped.

```mermaid
flowchart LR
    accTitle: L4 Detail -- Group sync decision
    accDescr: A remote group linked to a stored group through this provider updates that group. One with an id linked to another provider is skipped as provider_mismatch. Otherwise a free name creates a linked group, and a name already held is skipped as name_taken.

    linked{"Linked here?"}
    other{"Id linked elsewhere?"}
    update(["Update description"])
    free{"Name free?"}
    mismatch(["Skip provider_mismatch"])
    create(["Create, linked"])
    taken(["Skip name_taken"])

    linked -->|no| other
    linked -->|yes| update
    other -->|no| free
    other -.->|yes| mismatch
    free -->|yes| create
    free -.->|no| taken

    classDef default fill:#F8FAFC,stroke:#64748B,stroke-width:1px,color:#0F172A
    classDef decision fill:#FCF1D6,stroke:#9A6700,stroke-width:1px,color:#3D2A00
    classDef error fill:#FBE3D6,stroke:#C2410C,stroke-width:1px,color:#4A1A05
    class linked,other,free decision
    class mismatch,taken error
```

*Figure 2. L4 Detail -- Group sync decision. Dotted arrows end in a skip.* Up: [rbac.md](rbac.md).

A provider that is disabled, or not in `api` mode, is skipped before the listing. The sync updates a stored group when it is linked to that IdP group: its `source_id` is the IdP group's id and its `source_provider` is this provider, or one `auth.source_provider_bindings` joins to it. The directory assigns that id, so a user cannot choose it the way they choose a display name. Okta is the exception: its groups claim holds names, so an Okta group's id here is its name ([idp-setup/okta.md](idp-setup/okta.md)).

A group the sync creates is linked in the same write. A linked group keeps its roles, and the sync refreshes its description and nothing else. Admins assign roles to synced groups by hand.

A name match links nothing. Directory display names are not unique, and in a tenant where users can create groups anyone can pick one, so an IdP group called `DFE Admins` would otherwise take the seeded `dfe-admins` and its `admin` role. A stored group of the same name with no `source_id`, or holding another IdP group's id, is left untouched and counted as `name_taken`. So is a group the sync would create when its name is a stored group's `source_id`, or its id is a stored group's name.

A sync never moves a group between providers. Two providers can assign one id, and Okta's ids are group names, so an Okta group called `SOC Analysts` has the same id as a Keycloak group of that name. A stored group holding the IdP group's id but linked to another provider, whatever it is called, is left untouched and counted as `provider_mismatch`. The sync never creates a second group with that id, and the engine log names the syncing provider, the provider the group is linked to and the reason. `auth.source_provider_bindings` declares two names one identity source, either way round, so with `{"scim": "entra"}` the `entra` sync refreshes a group SCIM stamped `scim` and leaves the stamp as it is.

## Linking an IdP group

Linking an IdP group to a stored group is an admin's act. Set both `source_provider`, the provider's name in the registry, and `source_id`, the IdP group's id, through the groups API (`POST` or `PUT /api/v1/auth/groups`), dfe-ui's group form, the group's file in the live store or dfe-infra's `authConfig.groupsConfigMap` value. `PUT /api/v1/scim/v2/Groups/{name}` sets `source_id` from `externalId`. On a group not yet linked it sets `source_provider` to `scim` as well, so an OIDC sign-in reaches the group through the provider `auth.source_provider_bindings` binds to `scim` and no other. A group linked before keeps its provider.

The groups API turns down a link no login could use to reach the group alone:

- `409 conflict`: another group holds the `source_id`, under any provider (SCIM answers `uniqueness`).
- `409 identifier_taken`: a group's name is another group's `source_id`, or a changed `source_id` is another group's name (SCIM answers `uniqueness` for a `displayName` or `externalId`). An API key resolves each group identifier by name, then by `source_id`, so an identifier in both namespaces would let a key minted for one group take the other's roles once the first is deleted. A group with its name as its `source_id` is one group and is accepted. A pair stored before this check is logged as a warning at startup, naming both groups.
- A caller that cannot see every group gets one answer to either collision, `409 conflict` with `Group name '<name>' is not available`. A group outside the caller's scope answers `404` on get, update, member changes and delete, exactly as a missing one does.
- `422 invalid_source_id`: the `source_id` is over 512 characters, starts or ends with whitespace or holds a control character.
- `422 invalid_source_provider`: the `source_provider` is over 512 characters or is not an OIDC provider, a binding, `scim` or, when `auth.trust_proxy_auth_headers` is on, `auth.proxy_provider`.
- `422 missing_source_provider`: a changed link has a `source_id` but no `source_provider`.

A changed value is the one checked, so an edit to other fields of a group with a stored link these rules would turn down still succeeds. Setting, changing or clearing a link needs `group:write` at system scope, checked before any other group is read, and the role rights adding a member does, since each changes who holds the group's roles. Deleting a linked group needs them as well, through the groups API and SCIM alike.

## Account and group writes

An account write is checked against the groups the account holds before and after it. An IdP-owned account's group list is its assertion as well, so a name in that list that is another group's `source_id` is checked as that group as well.

A group write, through the groups API or SCIM, changes the group's member list and nothing else. It updates a local account's `groups` list to match but never an IdP-owned account's (one with `source_provider` set): that list is what the IdP asserts, so a group name written there would be read as an assertion. The member still holds the group through the member list, which SCIM `/Users` reports.

## Failed and partial syncs

One bad group never aborts the sync. A provider group with no id, a name or id that makes no valid group name, a name held by a stored group that does not load or is not linked to it, or a stored group linked to another provider is skipped and counted on `auth_oidc_sync_groups_skipped_total{reason}` (`no_id`, `invalid_name`, `stored_unloadable`, `name_taken`, `provider_mismatch`). The provider's `last_sync_status` then reads `partial` with each reason. A group with no id can never be linked, from any provider, so the engine log counts those in one line per run and names none of them.

A listing that fails on any page, the first or the last, stops the sync before it writes a group. So does a page in the wrong format, and a listing still handing out pages after 1,000. `last_sync_status` reads `error`, and `sync_error` says what failed: the provider's HTTP status, a page it could not read, or the page limit. A 429 reads as throttling, not as a refusal. The engine log has the provider's error code, and the next run lists again from the start.

A credential that is set but gives no access fails the same way. Entra issuing no token for the client secret, a Google service account JSON that does not build a client, or Google's token endpoint turning the service account down (`invalid_grant`) reads `error`, and `sync_error` says the directory API could not use the configured credential. The engine log has the reason. An Entra that cannot be reached, throttles the token request (429) or fails it (5xx) says that instead, and never blames the credential. `GET /oidc-providers/{name}/test` reports an Entra credential the same way, and keeps "not configured" for one that was never set.

## Sync outcomes

An api-mode Entra or Okta provider with no directory credential, or a Google provider with a configured service account that resolves to nothing, has nothing to list with. Its `last_sync_status` reads `not_configured`, `sync_error` says no credential is configured, and `POST /oidc-providers/{name}/sync` answers with the same text in `skipped`. A Google provider with no service account reads `user_token` with an empty `sync_error`, because each sign-in reads that user's groups with the user's token.

A credential behind a secret store that does not answer reads `error`, and `sync_error` names the secret store. One behind a secret store that turns the engine away (a 403 on the path, or a failed AppRole login) reads `error` as well, and `sync_error` opens "The secret store refused access", so the fix is the engine's secrets policy rather than the store's reachability. A `generic` provider has no directory API, so one set to api mode reads `error`, and `sync_error` says to set `groups.mode` to `token_claim`.

The sync answer's `status` field holds the outcome: `ok`, `partial`, `error`, `not_configured`, `user_token` or `skipped`. The scheduled sync counts each run on `auth_oidc_sync_scheduled_runs_total{outcome}`. A provider that is not configured is tried again after its `sync_interval`, so adding the credential is picked up without a restart.
