# DFE Expression Language — CEL

**Status:** Adopted
**Last Updated:** 2026-03-03

---

## Decision

DFE adopts **CEL (Common Expression Language)** as the unified expression language across all components — Python engine, Rust services, and CLI tooling.

**Why CEL:**

- **Purpose-built** for evaluating conditions against structured data (filtering, matching, policy)
- **Same implementation in both languages** — Rust crate `cel-interpreter` v0.10 powers both Rust services and Python (via `common-expression-language` PyO3 bindings). Zero behavioural drift.
- **Bounded execution** — not Turing-complete, guaranteed termination. No loops, no recursion, no side effects. Critical for per-row evaluation in data pipelines.
- **Compile-once, evaluate-many** — parse expression to AST once, evaluate against millions of rows
- **Industry standard** — Kubernetes admission control, Envoy proxy, Firebase Security Rules, Google Cloud IAM
- **Extensible** — custom functions can be registered for domain-specific needs

**Packages:**

| Language | Package | Version | Notes |
|----------|---------|---------|-------|
| Rust | `cel-interpreter` | 0.10 | Native implementation |
| Python | `common-expression-language` | >=0.5.6 | PyO3 wrapper of same Rust crate |

**References:**

- [CEL Specification](https://github.com/google/cel-spec)
- [CEL Overview](https://cel.dev/overview/cel-overview)
- [cel-interpreter crate](https://crates.io/crates/cel-interpreter)
- [Python CEL package](https://pypi.org/project/common-expression-language/)
- [FOSDEM 2026 — CEL in Rust](https://fosdem.org/2026/schedule/event/DBGZAU-rust-cel/)

---

## What CEL Replaces

| Component | Before | After |
|-----------|--------|-------|
| Hunt scoring `when` conditions | Hand-rolled regex parser in `scoring.py` | CEL via `scalo.expression` |
| Alert trigger evaluation | Hardcoded operator dict in `alert.py` | CEL conditions |
| dfe-loader transform conditions | Custom `if(condition, ...)` parser | CEL via `scalo::expression` (scalo-rs) |
| Routing rules (future) | Not yet implemented | CEL from day one |

## What CEL Does NOT Replace

| System | Reason |
|--------|--------|
| `@directive: body` DSL (`@source`, `@generated`, etc.) | Schema metadata — column mapping instructions, not boolean conditions |
| ClickHouse SQL in `@generated` expressions | Runs in ClickHouse, not our evaluator |
| Hunt SQL queries / rule_rewriter | ClickHouse-native SQL |
| Duration strings (`"1h"`, `"30m"`) | Simple config values, not expressions |

---

## DFE Expression Profile

DFE uses a **subset** of CEL. Expressions outside this profile are rejected at validation time.

### Allowed Operations

| Category | Operations | Examples |
|----------|-----------|---------|
| Comparison | `==`, `!=`, `<`, `<=`, `>`, `>=` | `severity == "critical"` |
| Logical | `&&`, `\|\|`, `!` | `amount > 1000 && !is_test` |
| Membership | `in` | `status in ["active", "pending"]` |
| Arithmetic | `+`, `-`, `*`, `/`, `%` | `price * quantity` |
| Ternary | `? :` | `is_admin ? 95 : 50` |

### Allowed Functions

| Function | Purpose | Example |
|----------|---------|---------|
| `contains()` | Substring check | `message.contains("error")` |
| `startsWith()` | Prefix check | `path.startsWith("/api/")` |
| `endsWith()` | Suffix check | `file.endsWith(".log")` |
| `matches()` | Regex match | `hostname.matches("^web-[0-9]+$")` |
| `size()` | Length of string or list | `size(tags) > 0` |
| `has()` | Field existence | `has(event.user_id)` |
| `int()` | Cast to integer | `int(port_str) == 443` |
| `double()` | Cast to float | `double(score_str) > 0.5` |
| `string()` | Cast to string | `string(code) == "200"` |
| `bool()` | Cast to boolean | `bool(enabled)` |

### Excluded (Rejected at Validation)

| Feature | Reason |
|---------|--------|
| `map()`, `filter()`, `exists()`, `all()` | Per-element iteration — unpredictable performance on large lists |
| `timestamp()`, `duration()` | ClickHouse handles time natively — no need to duplicate |
| Macros | Complexity with no benefit for DFE use cases |

---

## Supported Types

| CEL Type | Python | Rust | ClickHouse |
|----------|--------|------|------------|
| `int` | `int` | `i64` | `Int64` |
| `double` | `float` | `f64` | `Float64` |
| `string` | `str` | `String` | `String` |
| `bool` | `bool` | `bool` | `Bool` |
| `list` | `list` | `Vec<Value>` | `Array` |
| `map` | `dict` | `HashMap<String, Value>` | — |
| `null` | `None` | `Option::None` | `Nullable` |

---

## Usage Examples

### Hunt Scoring

```yaml
scoring:
  base_score: 50
  factors:
    - when: 'severity == "critical"'
      add: 25
    - when: "amount > 10000"
      multiply: 1.5
    - when: 'status in ["active", "escalated"]'
      add: 10
    - when: "!has(assignee)"
      add: 5
```

### Alert Triggers

```yaml
alerts:
  triggers:
    - when: "result_count >= 10"
    - when: 'severity == "critical" && result_count > 0'
    - when: "score > 80"
```

### Routing Rules (Future)

```yaml
routing:
  rules:
    - when: 'source.type == "syslog" && facility in [1, 2, 3]'
      target: syslog_table
    - when: 'message.contains("audit")'
      target: audit_table
```

### Computed Values (Transforms)

```yaml
columns:
  risk_label:
    expr: '@computed: risk_score > 80 ? "high" : risk_score > 40 ? "medium" : "low"'
```

---

## CEL-to-ClickHouse SQL Transpilation

CEL expressions can be mechanically converted to ClickHouse WHERE clauses for query pushdown:

| CEL | ClickHouse SQL |
|-----|---------------|
| `severity == "critical"` | `severity = 'critical'` |
| `amount > 10000 && !is_test` | `amount > 10000 AND NOT is_test` |
| `status in ["active", "pending"]` | `status IN ('active', 'pending')` |
| `message.contains("error")` | `position(message, 'error') > 0` |
| `path.startsWith("/api/")` | `startsWith(path, '/api/')` |
| `hostname.matches("^web-[0-9]+$")` | `match(hostname, '^web-[0-9]+$')` |
| `has(user_id)` | `user_id IS NOT NULL` |
| `size(tags) > 0` | `length(tags) > 0` |

This enables expressions written once in YAML to work both as in-memory evaluation (Rust/Python) and as ClickHouse query filters.

---

## Implementation Architecture

```
┌─────────────────────────────────────────────┐
│              DFE Expression Profile          │
│         (subset of CEL, documented here)     │
└─────────┬───────────────────┬───────────────┘
          │                   │
    ┌─────▼──────┐    ┌──────▼──────┐
    │  scalo-py   │    │  scalo-rs    │
    │ expression/ │    │ expression/  │
    │             │    │              │
    │ wraps:      │    │ wraps:       │
    │ common-     │    │ cel-         │
    │ expression- │    │ interpreter  │
    │ language    │    │ crate        │
    └─────┬──────┘    └──────┬──────┘
          │                   │
    ┌─────▼──────┐    ┌──────▼──────┐
    │ dfe-engine  │    │ dfe-loader   │
    │ scoring.py  │    │ transformer  │
    │ alert.py    │    │ .rs          │
    └─────────────┘    └─────────────┘
```

Both wrappers enforce the same DFE profile — identical allowed operators, functions, and types. The Rust crate is the single source of truth for parsing and evaluation semantics.

### API Surface (Both Languages)

```
compile(expr) → Program          # Parse + validate + compile to AST
evaluate(expr, data) → Any       # One-shot convenience (compile + execute)
validate(expr) → list[str]       # Syntax + profile check, returns errors for UI
```

---

## Migration Notes

### Syntax Changes from Previous Hand-Rolled Evaluator

| Before (scoring.py) | After (CEL) | Notes |
|---------------------|-------------|-------|
| `severity == 'critical'` | `severity == "critical"` | CEL prefers double quotes (single also works) |
| `amount > 10000` | `amount > 10000` | No change |
| `status in ('active', 'pending')` | `status in ["active", "pending"]` | CEL uses `[]` for lists |
| `status not in ('x', 'y')` | `!(status in ["x", "y"])` | CEL uses `!()` for negation |
| `enabled == true` | `enabled == true` | No change |
| `a == 1 (single condition only)` | `a == 1 && b == 2` | CEL supports compound conditions |

### Missing Field Behaviour

- **Previous:** `evaluate_condition()` returns `False` if field missing from data dict
- **CEL:** Accessing a missing field is a runtime error
- **Resolution:** Use `has()` for optional fields: `has(event.user_id) && event.user_id == "admin"`
- **Or:** Provide default values in the evaluation context
