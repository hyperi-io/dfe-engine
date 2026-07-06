# Third-Party Notices

This file records third-party software whose code or design influenced parts of
dfe-engine, per the applicable licences. dfe-engine itself is licensed BUSL-1.1
((c) HYPERI PTY LIMITED); the notices below cover external material only.

## PostHog (MIT) - ClickHouse attribution + error taxonomy patterns

Parts of the canonical ClickHouse access layer adapt patterns from PostHog's
open-source `posthog/clickhouse/` package:

- `src/dfe_engine/clickhouse/attribution.py` - the per-query attribution model held
  in a `contextvars.ContextVar`, the `tags_context()` scoping, and the
  `settings["log_comment"]` injection are adapted from PostHog's `query_tagging.py`
  + `client/execute.py`.
- `src/dfe_engine/clickhouse/errors.py` - the "classify into a coarse category +
  wrap into a typed, user-safe error" split is adapted from PostHog's `errors.py`.

  Copyright (c) PostHog Inc.
  Licensed under the MIT License. <https://github.com/PostHog/posthog>
  (Only the MIT-licensed `posthog/**` was used; the separately-licensed `ee/**`
  Enterprise code was NOT used.)

The MIT License permits this reuse with attribution; this notice is that
attribution. No verbatim files were copied - the patterns were re-implemented to
fit dfe-engine's structure.

## Sentry Snuba (FSL-1.1-Apache-2.0) - design inspiration only

The shape of the connection cache, the per-query settings-profile enum, and the
table-engine abstraction in `src/dfe_engine/clickhouse/{connection,profiles,engines}.py`
draw on the DESIGN of Sentry Snuba's `clusters/cluster.py`, `clickhouse/native.py`,
and `migrations/table_engines.py`.

  Copyright (c) Functional Software, Inc. (Sentry)
  Snuba is licensed under the FSL (converts to Apache-2.0 after two years).
  <https://github.com/getsentry/snuba>

Ideas / architecture are not copyright; NO Snuba source was copied verbatim into
dfe-engine (BUSL-1.1). The patterns were re-implemented independently. If any Snuba
source is ever lifted directly, an FSL-vs-BUSL-1.1 licence review is required first.
