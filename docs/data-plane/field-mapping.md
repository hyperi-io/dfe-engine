# Field mapping layer

A generic, standard-agnostic layer translating security/observability
standards (Sigma, Elastic ECS, Splunk CIM) to DFE schema columns. Queries
written in a standard's vocabulary run transparently against DFE tables
through generated views - the Sigma converter is a CONSUMER of this layer,
not the owner of mapping logic.

The mapping for a column is the first layer that names it.

| Priority | Layer | Description |
|----------|-------|-------------|
| 1 (highest) | inline `custom_mappings` | per-source overrides on the source's view entry - win per key |
| 2 | registry override map | the view's `field_map` pin (`"{standard}/{name}"` or bare name), else the source-name convention |
| 3 | default map | the standard's `_default` map (standard-wide defaults) |
| 4 (lowest) | passthrough | field name used as-is |

A `field_map` pin replaces the source-name convention for the override
layer, so several sources can share one map. A view with inline mappings and
no pin renders with no registry at all.

Maps are YAML files under `fieldmaps/` in the config directory, editable by the
engine, the console or a person. The resolver is
`src/dfe_engine/fieldmap/resolver.py` and the view generator
`src/dfe_engine/fieldmap/view_generator.py`.

Each standard and source pair produces a ClickHouse VIEW named
`{table}_{standard}` (e.g. `windows_audit_sigma`) that aliases DFE columns
back to the standard's field names:

```sql
CREATE OR REPLACE VIEW {db}.windows_audit_sigma AS
SELECT
    `source_ip` AS `SourceIP`,
    `user_name` AS `User`,
    *
FROM {db}.windows_audit;
```

A view holds the mappings whose column the source's table has, and drops the rest. ClickHouse refuses a whole view when one of its columns is missing, and a default map names far more columns than any one table has, so each dropped one is logged at debug. A view left with no mapping is not created. The source build, plan, deploy and the sigma view preview all apply the same rule.

Zero storage overhead - views compute at query time. Consumers:
[hunt-runner-scaling.md](hunt-runner-scaling.md) (detections),
[query-api.md](query-api.md), and the Observe explore UI.
