#  Project:      dfe-engine
#  File:         src/dfe_engine/cel/__init__.py
#  Purpose:      CEL expression utilities (classifier, validator, syntax checker)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""CEL expression utilities for dfe-engine.

Generic CEL handling used by:

- **Transport filters** — classify expressions into performance tiers,
  validate against tier gates
- **CEL field-mapping rules** — pre-validate before pushing to runtime
- **UI live validation** — TS UI POSTs CEL strings, gets back syntax errors
  and tier classification in real-time (~milliseconds, no Rust roundtrip)
- **Sigma/hunt rule transformation** — when CEL is used as a transformation
  predicate inside hunts/sigma, validate before deploying

The classifier mirrors the Rust implementation in
``scalo::transport::filter::classify`` byte-for-byte. This means
the UI validates with the same rules the runtime engine enforces — no
divergence between "looks valid in UI" and "rejected at startup".

## Tier Classification

CEL expressions are classified into three performance tiers:

| Tier | Description | Cost | Examples |
|------|-------------|------|----------|
| 1    | SIMD field ops (no CEL engine) | ~50-200 ns | `has(field)`, `f == "x"`, `f.startsWith("p")` |
| 2    | Standard CEL | ~500ns-1us | `severity > 3 && source != "internal"` |
| 3    | Complex CEL (regex, iteration, time) | ~5-50us | `f.matches("^p.*")`, `tags.exists(t, t == "x")` |

## Example

```python
from dfe_engine.cel import classify_expression, FilterTier

result = classify_expression('has(_table)')
assert result.tier == FilterTier.TIER1

result = classify_expression('severity > 3 && source != "internal"')
assert result.tier == FilterTier.TIER2
assert result.fields == ['severity', 'source']
```
"""

from dfe_engine.cel.classify import (
    ClassifyResult,
    FilterTier,
    Tier1Op,
    classify_expression,
)
from dfe_engine.cel.syntax import (
    SyntaxCheckResult,
    check_syntax,
)

__all__ = [
    "ClassifyResult",
    "FilterTier",
    "SyntaxCheckResult",
    "Tier1Op",
    "check_syntax",
    "classify_expression",
]
