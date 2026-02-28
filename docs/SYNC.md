# Cross-Repo Synchronisation

**Status:** Proposed
**Last Updated:** 2026-02-28

---

## Problem

Two repos define overlapping schema structures:

| Concept | dfe-engine (Python) | dfe-loader (Rust) |
|---------|--------------------|--------------------|
| Common header fields | `type_maps.csv` + schema CSVs | `schemas/profiles/*.yaml` + `COMMON-HEADER.md` |
| Field types | `type_maps.csv` (24 logical types) | `src/clickhouse/types.rs` (runtime parsing) |
| Loader directives | Column `comment` in schema CSV | `DDL-EXPRESSION.md` + `field_mapping.rs` |
| Profile definitions | Not yet (proposed in SOURCE.md) | `schemas/profiles/timeseries.yaml` etc. |

If these drift apart, the engine generates DDL that the loader can't
correctly populate — or the loader expects columns that don't exist.

---

## What Must Stay In Sync

### 1. Common Header Profile Definitions

The common header is the **contract** between the engine (which generates
DDL) and the loader (which populates fields). Both must agree on:

- Field names (`_timestamp`, `_org_id`, `_source`, etc.)
- Field types (exact ClickHouse types)
- Field order in ORDER BY
- Loader directives (how each field is populated)
- Profile versioning

### 2. Type Primitives → ClickHouse Type Mapping

The engine maps primitives to ClickHouse types when generating DDL.
The loader parses ClickHouse types from `system.columns` at runtime.
These must be compatible — the engine's output must be valid input for
the loader's type parser.

### 3. Loader Directive Syntax

The engine writes `@source`, `@generated`, `@renamed`, `@captured`,
`@computed` directives as column comments. The loader parses these
directives. The syntax must match.

---

## Synchronisation Strategy

### Single Source of Truth: dfe-engine

dfe-engine owns the **canonical definitions**:

- Common header profile YAML files
- Type primitive → ClickHouse type mapping
- Schema CSV format specification
- Loader directive syntax (documented, not parsed by engine)

dfe-loader is a **consumer** of these definitions. It reads column
metadata from ClickHouse at runtime (`system.columns`), not from
config files. The contract is the DDL itself — whatever the engine
produces, the loader must handle.

### Shared Artefact: Profile YAML

Profile definitions (timeseries, minimal, passthrough) are the most
critical shared artefact. Strategy:

```
dfe-engine (source of truth)
  │
  │  profiles/timeseries.yaml
  │  profiles/minimal.yaml
  │  profiles/passthrough.yaml
  │
  ├──▶ dfe-engine uses profiles to generate DDL
  │
  └──▶ dfe-loader ships the SAME profile YAMLs
       (copied or git-submodule'd from dfe-engine)
       used for auto-init default table creation
```

**Options for sharing:**

| Approach | Pros | Cons |
|----------|------|------|
| **Git submodule** | Always in sync, single edit point | Submodule ceremony, two-step update |
| **Shared package** | Clean dependency, versioned | Extra package to publish |
| **Copy + CI check** | Simple, no tooling | Can drift, needs CI enforcement |
| **Monorepo** | Zero sync problem | Not current repo structure |

**Recommended: Git submodule** pointing to a `schemas/` directory in
dfe-engine. dfe-loader pins to a tag/commit. CI validates that the
loader's pinned profiles match the engine's current profiles.

### Runtime Contract: Deployed ClickHouse Schema is the ONLY SSoT

The Rust K8s services (loader, archiver) at scale **slave from the
deployed ClickHouse schema ONLY**. They do NOT read profile YAMLs,
Source definitions, or any config files at query/ingest time. They read:

1. **Column types** from `system.columns` — the deployed table
2. **Column comments** from `system.columns` — contain loader directives
3. **Table comment** — contains `@profile`, `@profile_version` tags

This means there is **never an in-flight mismatch**. The flow is strictly
one-directional:

```
Source YAML → engine generates DDL → deploys to ClickHouse → Rust services read from ClickHouse
                                                                    ↑
                                                          ONLY source of truth at runtime
```

The engine writes, ClickHouse stores, Rust services read. No intermediate
state, no config sync, no cache invalidation race. If the engine hasn't
deployed a schema change yet, the Rust services continue operating on the
current deployed schema. Once the DDL is applied, the Rust services pick
up the change on their next `system.columns` refresh (TTL-based cache).

Profile YAMLs are only needed for the loader's **auto-init** feature
(creating the default table when no table exists). Once the table is
created, the YAML is irrelevant — the deployed DDL is the truth.

### CI Validation

Add a CI step that:

1. Generates DDL from dfe-engine profiles
2. Parses the generated DDL with dfe-loader's type parser
3. Validates that all column comments contain valid loader directives
4. Fails if any incompatibility is detected

```yaml
# .github/workflows/schema-compat.yml
- name: Generate DDL from profiles
  run: python -m dfe_engine.schema.generate_profiles --output /tmp/ddl/

- name: Validate loader compatibility
  run: cargo test --manifest-path ../dfe-loader/Cargo.toml -- schema_compat
  env:
    DDL_DIR: /tmp/ddl/
```

This catches drift at PR time, not in production.

---

## Version Contract

Both repos must agree on version numbers:

```
@profile: timeseries
@profile_version: 1
@schema_version: 2
```

When the engine bumps a profile version:
1. Engine generates migration DDL (`ALTER TABLE ADD COLUMN`)
2. Loader's `ProfileDiff` detects the version mismatch and applies migration
3. CI validates compatibility before merge

The version is embedded in the table comment — the loader reads it at
startup and logs a warning if it's behind the current profile version.

---

## What Each Repo Owns

| Responsibility | Owner | Notes |
|---------------|-------|-------|
| Profile definitions (YAML) | dfe-engine | Canonical source |
| Type primitive mapping | dfe-engine | `type_maps.csv` or equivalent |
| DDL generation | dfe-engine | `SchemaBuilder` / `ClickHouseSchema` |
| Loader directive syntax | dfe-loader | Defines the `@` expression language |
| Loader directive documentation | dfe-engine | Documents what directives are available |
| Column comment parsing | dfe-loader | `field_mapping.rs` |
| Schema caching | dfe-loader | TTL-based from `system.columns` |
| Auto-init DDL | dfe-loader | Uses profile YAMLs (shared from engine) |
| Profile migration | dfe-loader | `ProfileDiff` generates ALTER statements |
| CI compatibility check | Both | Cross-repo CI step |

---

## Monorepo Path

If/when the repos merge into a monorepo (see [MONOREPO-MIGRATION.md](./MONOREPO-MIGRATION.md)),
the sync problem disappears. Profile YAMLs live in one place, CI runs
both the Python and Rust tests in a single pipeline, and there's no
submodule or copy-on-release ceremony.

The submodule approach is designed to be a stepping stone — when the
monorepo happens, the submodule reference is replaced by a direct path.
