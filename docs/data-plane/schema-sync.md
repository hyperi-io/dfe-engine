# The engine and loader schema contract

The engine renders and applies the DDL. dfe-loader reads the deployed table back out of ClickHouse and creates none. So the deployed table is the whole contract between them, and dfe-schemas is where the table's definition comes from.

## dfe-schemas owns the definitions

The dfe-schemas package holds the common-header profiles (`common-header/`), the source meta schemas (`meta/`), the hunt output schemas (`hunts/`) and the registries (`registries/`): table engines, the type system (primitive to ClickHouse type) and the default field maps.

The engine reads the version-pinned `dfe-schemas` wheel through `importlib.resources` and ships no schema files. It looks in `DFE_ENGINE_SCHEMAS_DIR`, then the installed package, then the image's seed directory (copied out of that wheel at build), and when none has what it needs, loading fails with `No dfe-schemas tree carries ...: set DFE_ENGINE_SCHEMAS_DIR or install dfe-schemas` (`src/dfe_engine/schema/schema_loader.py`).

## The deployed table is the runtime contract

The engine applies the DDL, and dfe-loader reads the table back from `system.columns`.

```mermaid
flowchart LR
    accTitle: L4 Detail -- Engine and loader schema contract
    accDescr: The dfe-schemas wheel and the source definition feed the engine. The engine renders the DDL and applies it to the ClickHouse table. dfe-loader reads the table's columns and comments back from system.columns.

    subgraph defs["Definitions"]
        schemas["dfe-schemas<br/>wheel"]
        source["Source<br/>definition"]
    end

    engine["dfe-engine"]

    table[("ClickHouse<br/>table")]

    loader["dfe-loader"]

    defs --> engine
    engine -->|DDL| table
    table -->|system.columns| loader

    classDef default fill:#F8FAFC,stroke:#64748B,stroke-width:1px,color:#0F172A
    classDef store fill:#DDF3EC,stroke:#00785A,stroke-width:1px,color:#0B3327
    classDef group fill:none,stroke:#64748B,stroke-width:1px,stroke-dasharray:2 4
    class table store
    class defs group
```

*Figure 1. L4 Detail -- Engine and loader schema contract.* Up: [Data plane](README.md).

dfe-loader reads three things from the table, never a profile YAML or a source definition:

1. Column types, from `system.columns`.
2. Column COMMENTs, which hold the loader directives.
3. The table COMMENT, which holds `@profile` and `@profile_version` for a header-composed table (dfe-schemas `render.py`).

The loader caches that schema for `schema.cache_ttl_secs` (300 by default, dfe-loader `docs/config-schema.yaml`), so an applied change reaches it on the next refresh, and until then it keeps using the schema it holds.

## What has to agree

- Directive vocabulary. The loader parses `@skip`, `@default`, `@source`, `@renamed`, `@computed` and `@coerce` (dfe-loader `src/column_meta/mod.rs`). `tests/unit/test_services/test_loader_directive_contract.py` restates that list as a literal and fails when the engine writes a directive the loader does not parse.
- Types. The engine maps primitives through dfe-schemas `registries/types.yaml`. The loader parses whatever ClickHouse type `system.columns` reports.
- Header columns. Both sides take them from the profile in `common-header/`.

No cross-repo CI job runs the loader's type parser over engine-generated DDL, and the loader has no profile migration: the engine's schema apply adds the columns a new profile version brings.
