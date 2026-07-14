# Field mapping layer

A generic, standard-agnostic layer translating security/observability
standards (Sigma, Elastic ECS, Splunk CIM) to DFE schema columns. Queries
written in a standard's vocabulary run transparently against DFE tables
through generated views - the Sigma converter is a CONSUMER of this layer,
not the owner of mapping logic.

## Resolution order

| Priority | Layer | Description |
|----------|-------|-------------|
| 1 (highest) | inline `custom_mappings` | per-source overrides on the source's view entry - win per key |
| 2 | registry override map | the view's `field_map` pin (`"{standard}/{name}"` or bare name), else the source-name convention |
| 3 | default map | the standard's `_default` map (standard-wide defaults) |
| 4 (lowest) | passthrough | field name used as-is |

A `field_map` pin replaces the source-name convention for the override
layer, so one named map can be shared across many sources; a view with only
inline mappings renders even without a registry.

Maps are YAML in the config repo (engine-, UI-, or human-editable). The
resolver lives at `src/dfe_engine/fieldmap/resolver.py`; view generation at
`src/dfe_engine/fieldmap/view_generator.py`.

## Generated views

Each standard x source produces a ClickHouse VIEW named
`{standard}_{source}` (e.g. `sigma_windows_audit`) that aliases DFE columns
back to the standard's field names:

```sql
CREATE OR REPLACE VIEW {db}.windows_audit_sigma AS
SELECT
    `source_ip` AS `SourceIP`,
    `user_name` AS `User`,
    *
FROM {db}.windows_audit;
```

Zero storage overhead - views compute at query time. Consumers:
[hunt-runner-scaling.md](hunt-runner-scaling.md) (detections),
[query-api.md](query-api.md), and the HyperDX explore UI.
