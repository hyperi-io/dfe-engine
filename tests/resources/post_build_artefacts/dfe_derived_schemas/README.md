# DFE Derived Schemas

This directory contains the derived schemas used by the DFE Data Engine. Each schema follows an immutable versioning pattern to ensure consistency and traceability.

## Directory Structure

```
dfe_derived_schemas/
├── {schema_category}/
│   └── schema_name/
│   │   └── {sub_schema_name}/
│   │       └── v{major}_{minor}_{patch}/
│   │           └── {sub_schema_name}_sub.csv
```

## Versioning Strategy

We use semantic versioning (MAJOR.MINOR.PATCH) for all schemas:

- MAJOR version increments represent breaking changes
- MINOR version increments represent backwards-compatible feature additions
- PATCH version increments represent backwards-compatible bug fixes

Each schema is versioned independently, allowing for granular control over schema evolution.

### Version Directory Format

Version directories follow the pattern: `v{major}_{minor}_{patch}`
Example: `v001_000_000`

## Build Process

1. Schema files are initially placed in their respective versioned directories
2. Each schema must include:
   - CSV schema definition file
   - Version directory following the semantic versioning pattern
   - Proper placement within the schema category hierarchy

## Schema Categories

- logs_beats_filebeat: Filebeat-related schemas for various log sources
- [Add other categories as needed]

## Version Control

- All schema changes must be version controlled
- New versions should be created rather than modifying existing versions
- Version history and changes are tracked in DERIVED_SCHEMAS.md in the GitBook documentation

## Quality Control

- All schemas must follow the defined directory structure
- Version numbers must be properly formatted
- CSV files must follow the schema specification
- Breaking changes must increment the major version number
