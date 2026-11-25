# The engine and loader schema contract

The engine renders and applies the DDL. dfe-loader reads the deployed table back out of ClickHouse and creates none. So the deployed table is the whole contract between them, and dfe-schemas is where the table's definition comes from.

## dfe-schemas owns the definitions

The dfe-schemas package holds the common-header profiles (`common-header/`), the source meta schemas (`meta/`), the hunt output schemas (`hunts/`) and the registries (`registries/`): table engines, the type system (primitive to ClickHouse type) and the default field maps.

The engine reads the version-pinned `dfe-schemas` wheel through `importlib.resources` and ships no schema files. It looks in `DFE_ENGINE_SCHEMAS_DIR`, then the installed package, then the image's seed directory (copied out of that wheel at build), and when none has what it needs, loading fails with `No dfe-schemas tree carries ...: set DFE_ENGINE_SCHEMAS_DIR or install dfe-schemas` (`src/dfe_engine/schema/schema_loader.py`).

## The deployed table is the runtime contract

The engine applies the DDL, and dfe-loader reads the table back from `system.columns`.

```mermaid
---
config:
  theme: base
  look: neo
  fontFamily: Inter, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif
  flowchart:
    curve: linear
    nodeSpacing: 40
    rankSpacing: 64
    padding: 18
    wrappingWidth: 280
    htmlLabels: true
  elk:
    preset: modelOrder
  themeVariables:
    fontSize: 15px
    lineColor: "#64748B"
    primaryTextColor: "#0F172A"
    clusterBorder: "#0072B2"
    edgeLabelBackground: "#FFFFFF"
  themeCSS: >-
    p{margin:0}
    .node[data-look] rect,.node[data-look] path,.node[data-look] polygon,.node[data-look] .outer-path{filter:drop-shadow(0 1px 2px rgba(15,23,42,.10)) drop-shadow(0 6px 14px rgba(15,23,42,.10))}
    .node rect{rx:10px;ry:10px}
    .cluster rect{rx:16px;ry:16px}
    .node .label div{min-width:150px}
    .node.decision .label div{min-width:0}
    .cluster[data-look] rect{filter:none}
    .nodeLabel{line-height:1.35}
    .nodeLabel b{font-weight:600;font-size:15px;letter-spacing:-0.01em}
    .nodeLabel span{font-size:12.5px;font-weight:400;color:#334155}
    .cluster-label span.nodeLabel{display:inline-block;background:#FFFFFF;color:#0B2A3F;border:1.5px solid #0072B2;border-radius:999px;padding:2px 12px;font-size:11.5px;font-weight:700;letter-spacing:.09em;text-transform:uppercase}
    .flowchart-link{stroke-width:1.75px}
    .labelBkg{background:transparent!important}
    span.edgeLabel{display:inline-block;background:#FFFFFF!important;color:#1E293B;border:1px solid #94A3B8;border-radius:999px;padding:2px 10px;font-size:12px;font-weight:500}
    span.edgeLabel p{background:transparent!important}
    .label-icon{width:20px;height:20px;vertical-align:-4px;margin-right:6px}
    .ingest .label-icon{color:#0E8A7D}
    .xform .label-icon{color:#7B4FB0}
    .load .label-icon{color:#0072B2}
    .bus .label-icon{color:#A8517F}
    .store .label-icon{color:#00785A}
    .ui .label-icon{color:#D9480F}
    .ext .label-icon{color:#64748B}
---
flowchart TB
  accTitle: L4 Detail -- Engine and loader schema contract
  accDescr: The dfe-schemas wheel and the source definition feed the engine. The engine renders the DDL and applies it to the ClickHouse table. dfe-loader reads the table's columns and comments back from system.columns.

  subgraph defs["Definitions"]
    direction LR
    schemas["<b>dfe-schemas</b><br/><span>wheel</span>"]
    source["<b>Source definition</b>"]
    schemas ~~~ source
  end

  engine["<b>dfe-engine</b>"]

  table[("<b>ClickHouse table</b>")]

  loader["<b>dfe-loader</b>"]

  defs --> engine
  engine -->|DDL| table
  table -->|system.columns| loader
  class loader load
  class schemas,table store
  class engine control
  class source part
  classDef ext fill:#F4F6F8,stroke:#94A3B8,stroke-width:1.5px,stroke-dasharray:5 4,color:#0F172A
  classDef ingest fill:#FFFFFF,stroke:#0E8A7D,stroke-width:3px,color:#0F172A
  classDef xform fill:#FFFFFF,stroke:#7B4FB0,stroke-width:3px,color:#0F172A
  classDef load fill:#FFFFFF,stroke:#0072B2,stroke-width:3px,color:#0F172A
  classDef bus fill:#FFFFFF,stroke:#A8517F,stroke-width:3px,color:#0F172A
  classDef store fill:#FFFFFF,stroke:#00785A,stroke-width:3px,color:#0F172A
  classDef ui fill:#FFFFFF,stroke:#D9480F,stroke-width:3px,color:#0F172A
  classDef decision fill:#FFFFFF,stroke:#9A6700,stroke-width:3px,color:#0F172A
  classDef error fill:#FFFFFF,stroke:#C2410C,stroke-width:3px,color:#0F172A
  classDef control fill:#FFFFFF,stroke:#334155,stroke-width:3px,color:#0F172A
  classDef part fill:#FFFFFF,stroke:#94A3B8,stroke-width:1.5px,color:#0F172A
  style defs fill:none,stroke:#0072B2,stroke-width:1.5px,stroke-dasharray:6 4
```

*Figure 1. L4 Detail -- Engine and loader schema contract.* Up: [Data plane](README.md).

dfe-loader reads three things from the table, never a profile YAML or a source definition:

1. Column types, from `system.columns`.
2. Column COMMENTs, which hold the loader directives.
3. The table COMMENT, which holds `@profile` and `@profile_version` for a header-composed table (dfe-schemas `render.py`).

The loader caches that schema for `schema.cache_ttl_secs` (300 by default, dfe-loader `docs/config-schema.yaml`). A source deploy moves the source's entry in the loader's `routing.table_schema_versions`, and the reload that follows drops that table's cached schema, so a column the deploy added fills from the next record. Any other applied change reaches the loader on the next refresh, and until then it keeps using the schema it holds.

## What has to agree

- Directive vocabulary. The loader parses `@skip`, `@default`, `@source`, `@renamed`, `@computed` and `@coerce` (dfe-loader `src/column_meta/mod.rs`). `tests/unit/test_services/test_loader_directive_contract.py` restates that list as a literal and fails when the engine writes a directive the loader does not parse.
- Types. The engine maps primitives through dfe-schemas `registries/types.yaml`. The loader parses whatever ClickHouse type `system.columns` reports.
- Header columns. Both sides take them from the profile in `common-header/`.

No cross-repo CI job runs the loader's type parser over engine-generated DDL, and the loader has no profile migration: the engine's schema apply adds the columns a new profile version brings.
