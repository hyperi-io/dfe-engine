#  Project:      dfe-engine
#  File:         src/dfe_engine/transport_filter/__init__.py
#  Purpose:      Transport filter configuration helpers for hyperi-rustlib
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Transport filter configuration helpers.

Mirrors the `hyperi_rustlib::transport::filter` module — provides Pydantic
models, CEL syntax classifier, and validation for transport-level message
filters. Used by the control plane to validate filter configs before they
reach rustlib at service startup.

See: /projects/hyperi-rustlib/src/transport/filter/

## Performance Tiers

- **Tier 1** — SIMD field extraction via memmem/sonic-rs (~50-200 ns/msg).
  Always enabled, no opt-in needed.
- **Tier 2** — Pre-compiled CEL with extracted fields (~500ns-1us/msg).
  Requires `expression.allow_cel_filters_in` or `allow_cel_filters_out`.
- **Tier 3** — Complex CEL with regex/iteration/time (~5-50 us/msg).
  Requires `expression.allow_complex_filters_in` or `allow_complex_filters_out`.

## Example

```python
from dfe_engine.transport_filter import (
    FilterRule, FilterAction, TransportFilterTierConfig,
    classify_expression, validate_filter_rules,
)

rules = [
    FilterRule(expression="has(_table)", action=FilterAction.DROP),
    FilterRule(expression='status == "poison"', action=FilterAction.DLQ),
]

tier_config = TransportFilterTierConfig()
errors = validate_filter_rules(rules, "in", tier_config)
if errors:
    raise ValueError(f"Invalid filter config: {errors}")
```
"""

# Tier classification lives in dfe_engine.cel (generic — used anywhere CEL appears)
from dfe_engine.cel import (
    ClassifyResult,
    FilterTier,
    Tier1Op,
    classify_expression,
)
from dfe_engine.transport_filter.models import (
    FilterAction,
    FilterRule,
    TransportFilterTierConfig,
)
from dfe_engine.transport_filter.validate import (
    FilterValidationError,
    validate_filter_rules,
)

__all__ = [
    "ClassifyResult",
    "FilterAction",
    "FilterRule",
    "FilterTier",
    "FilterValidationError",
    "Tier1Op",
    "TransportFilterTierConfig",
    "classify_expression",
    "validate_filter_rules",
]
