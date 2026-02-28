# DFE Engine - TODO

**Project Goal:** Production-ready core library for Data Fusion Engine

---

## WBS: Source + Schema v2 Implementation

Design docs agreed: [SOURCE.md](docs/SOURCE.md), [SCHEMA.md](docs/SCHEMA.md), [SYNC.md](docs/SYNC.md).
Since 2.2 is not live, all v1 removals are hard deletes — no deprecation.

### Phase 1: Foundation — Type Registry + Source Model

**1.1 Type Registry** (`src/dfe_engine/source/type_registry.py`)
- [ ] Create `type_registry.yaml` — primitive→CH type+codec+nullable mapping
- [ ] `TypeRegistry` class — loads/validates the YAML
- [ ] `resolve(primitive, attributes, use_case, ch_override)` → CH column DDL fragment
- [ ] `validate_use_case(primitive, use_case)` — enforces 1:M (SCHEMA.md table)
- [ ] `validate_attribute(primitive, attribute)` — enforces 1:M (SCHEMA.md table)
- [ ] CH override catalogue validation
- [ ] Unit tests

**1.2 Source Model** (`src/dfe_engine/source/models.py`)
- [ ] Pydantic `Source` model per SOURCE.md YAML structure
- [ ] Sub-models: `SourceHeader`, `SourceMatch`, `SourceSchema`, `SourceTransform`, `SourceFetcher`, `SourceSigma`
- [ ] `SchemaColumn` model: name, type, attribute (list), use_case, default, order, comment, ch_override
- [ ] Validators: _source naming rules, use_case↔primitive, attribute↔primitive (via TypeRegistry)
- [ ] YAML serialisation round-trip
- [ ] Unit tests

**1.3 Source Registry** (`src/dfe_engine/source/registry.py`)
- [ ] `SourceRegistry` class — mirrors `ServiceConfigRegistry` pattern
- [ ] DirectoryConfigStore backing, `sources/{source_name}.yaml` layout
- [ ] CRUD: get_source, save_source, delete_source, list_sources
- [ ] Validation: unique _source, no match conflicts
- [ ] Change callbacks, git-aware writes
- [ ] `SourceSettings` in settings.py (`DFE_SOURCES_DIR`)
- [ ] Unit tests (CRUD, validation, round-trip)

### Phase 2: Schema v2 — YAML→DDL Pipeline

**2.1 YAML Schema Loader** (rewrite `schema/schema_util.py`)
- [ ] Replace CSV loading with YAML loading (ruamel-yaml)
- [ ] Load meta_schema.yaml → `list[SchemaColumn]`
- [ ] Load derived_schema.yaml → merge/override
- [ ] Load additional_fields.yaml → append
- [ ] Remove pandas DataFrame dependencies from schema loading
- [ ] Common header profile loading (timeseries, minimal, passthrough) from YAML
- [ ] Unit tests

**2.2 DDL Generator** (rewrite `schema/schema_ch.py`)
- [ ] Rewrite `ClickHouseSchema` to consume `list[SchemaColumn]` (not DataFrames)
- [ ] Integrate TypeRegistry for primitive→CH type resolution
- [ ] Use case → index generation:
  - `dimension` → `set(0) GRANULARITY 4`
  - `fulltext` → `text(tokenizer=splitByNonAlpha) GRANULARITY 1` (fallback: tokenbf_v1)
  - `text_search` → `text(tokenizer=ngrams(3)) GRANULARITY 1` (fallback: ngrambf_v1)
  - `range` → `minmax GRANULARITY 4`
  - `bloom` → `bloom_filter GRANULARITY 4`
- [ ] Attribute application (lowcardinality, nullable, materialized, alias)
- [ ] ch_override passthrough (verbatim in DDL)
- [ ] Codec selection from TypeRegistry
- [ ] ORDER BY / PRIMARY KEY from `order` field
- [ ] PARTITION BY, TTL, ENGINE from Source schema config
- [ ] Table comment (@profile, @profile_version, @schema_version)
- [ ] Column comments with loader directives
- [ ] Unit tests (DDL output validation)

**2.3 Schema Builder** (rewrite `schema/schema_builder.py`)
- [ ] Load from Source definitions (not standalone CSV configs)
- [ ] Compose: common header profile + meta_schema + derived + additional
- [ ] Generate DDL via updated ClickHouseSchema
- [ ] Sigma view generation (CREATE VIEW with column aliases, per-source)
- [ ] Remove dfe_package.yaml dependency
- [ ] Unit tests

### Phase 3: Sigma + Hunts Integration

**3.1 Sigma Field Mapping** (rewrite `sigma/field_mapping_service.py`)
- [ ] Replace PG `meta_schemas`/`derived_schemas` queries with SourceRegistry lookups
- [ ] Load schema columns from Source YAML
- [ ] Replace compound type checks (`string_fast` etc.) with new primitives
- [ ] Remove sqlalchemy/db_session dependency
- [ ] Sigma view DDL generation
- [ ] Unit tests

**3.2 Sigma Converter** (update `sigma/sigma_converter.py`)
- [ ] Replace compound type references with new primitives
- [ ] Replace dfe_package.yaml loading with Source-based config
- [ ] Resolve Source by sigma `logsource` mapping
- [ ] Unit tests

**3.3 Hunts** (update `hunts/hunts/hunts.py`)
- [ ] Wire hunt rule loading to Source model (rule.source → SourceRegistry)
- [ ] Template variables: `{db}`, `{source}` resolved from Source
- [ ] Alerts table as a Source (timeseries profile)
- [ ] Update hunt validator for new schema format
- [ ] Unit tests

### Phase 4: Service Model Updates

**4.1 Receiver Model** (update `services/models/receiver.py`)
- [ ] Add `source_routing` mode alongside category_to_topic (transition)
- [ ] Compile match table from SourceRegistry
- [ ] Config generator: Source definitions → receiver match table
- [ ] Unit tests

**4.2 Loader Model** (update `services/models/loader.py`)
- [ ] Add `source_routing` mode alongside category_to_table (transition)
- [ ] `_source` field → `{db}.{_source}` table (direct routing)
- [ ] Config generator: Source definitions → loader routing config
- [ ] Unit tests

### Phase 5: Removal of v1 Code

**5.1 Delete `src/deprecated/`**
- [ ] Remove entire directory (config_loader, watcher_converter, old resources — 60 files)
- [ ] Remove imports referencing deprecated modules
- [ ] Verify no test imports from deprecated

**5.2 Delete `src/dfe_engine/schemas/`** (legacy module, NOT `schema/`)
- [ ] Remove schema.py, schema_builder.py, schema_utils.py, schema_ch_ddl_generator.py
- [ ] Remove schema_field_definitions/ directory
- [ ] Update imports that reference `dfe_engine.schemas`

**5.3 Delete old CSV resources**
- [ ] Remove `tests/resources/common/v001_*/type_maps.csv` (6 versions)
- [ ] Remove CSV schema test fixtures
- [ ] Update test conftest.py files

**5.4 Clean up compound type references**
- [ ] Remove all `string_fast`, `string_lowcardinality`, `string_fast_lowcardinality` references
- [ ] Update test fixtures to new primitives
- [ ] Remove OpenSearch type mapping columns

**5.5 Remove PostgreSQL schema storage**
- [ ] Remove sqlalchemy from sigma module
- [ ] Remove db_session parameters from field_mapping_service
- [ ] Remove PG table queries (meta_schemas, derived_schemas)
- [ ] Review settings.py postgres section (keep if query module still needs it)

### Phase 6: Tests + Validation

**6.1 New unit tests**
- [ ] Source model (creation, validation, serialisation)
- [ ] TypeRegistry (resolution, use_case validation, attribute validation, ch_override)
- [ ] SourceRegistry (CRUD, unique _source, match conflicts)
- [ ] Schema YAML loading (meta, derived, additional, profiles)
- [ ] DDL generation (full table, indexes, comments, ORDER BY)
- [ ] Sigma view generation

**6.2 Update existing tests**
- [ ] Schema build tests → YAML instead of CSV
- [ ] Sigma tests → new primitives, remove PG mocks
- [ ] Hunt tests → wire to Source model
- [ ] Pipeline tests → remove dfe_package.yaml dependency

**6.3 Integration tests**
- [ ] Source → Schema → DDL round-trip
- [ ] Source → Sigma view generation
- [ ] Source → Hunt rule execution (mock ClickHouse)

### Verification Checklist

- [ ] `pytest tests/unit/` — all pass
- [ ] `pytest tests/integration/` — all pass
- [ ] `ruff check src/` — clean
- [ ] `grep -r "string_fast" src/` — zero results
- [ ] `grep -r "type_maps.csv" src/` — zero results
- [ ] `grep -r "dfe_package" src/dfe_engine/` — zero results
- [ ] `src/deprecated/` does not exist
- [ ] `src/dfe_engine/schemas/` does not exist
- [ ] Manual: Source YAML → DDL matches SCHEMA.md examples

---

## Phase Dependencies

```
Phase 1 (Foundation)       ─── 1.1 → 1.2 → 1.3 (sequential)
Phase 2 (Schema v2)        ─── depends on Phase 1; 2.1 → 2.2 → 2.3
Phase 3 (Sigma + Hunts)    ─── depends on Phase 1 + 2; 3.1 → 3.2; 3.3 independent
Phase 4 (Service Models)   ─── depends on Phase 1 only; parallel with 2-3
Phase 5 (Removal)          ─── depends on Phases 2-4 (new replaces old)
Phase 6 (Tests)            ─── continuous; final validation after Phase 5
```

---

## Backlog (post-WBS)

### Architecture (decided, not yet implemented)
- [ ] Deploy HyperDX (ClickStack) as observability UI
- [ ] Implement HyperDX OIDC middleware (docs/oauth2/HYPERDX-MIDDLEWARE.md)
- [ ] Deploy Envoy Gateway with native OIDC (replaces nginx-ingress + oauth2-proxy)
- [ ] Argo CD interface layer
- [ ] OTEL metrics in Rust services (replace Prometheus)

### Refactoring
- [ ] Extract KEDA controls into dedicated module
- [ ] Extract Argo CD interface into dedicated module

### Future Enhancements
- [ ] Hunt query staggering (EXPLAIN cost estimation)
- [ ] Async ClickHouse operations
- [ ] Schema diff visualization
- [ ] Storage abstraction layer (local, S3, HTTP)

### Technical Debt
- [ ] Increase test coverage to 90%+
- [ ] Add type hints to all public APIs
- [ ] Fix ruff lint warnings in non-services modules
- [ ] SPDX-compliant headers on all source files

---

## Notes

- Use `uv` for all Python package management (NOT pip)
- Virtual environment: `.venv` (Python 3.12)
- See STATE.md for project status and architecture decisions
- Design docs: [SOURCE.md](docs/SOURCE.md), [SCHEMA.md](docs/SCHEMA.md), [SYNC.md](docs/SYNC.md)

---

**Last Updated:** 2026-02-28
