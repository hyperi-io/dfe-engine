<!--
  Project:   dfe-engine
  File:      docs/data-plane/throughput-analysis.md
  Purpose:   Throughput analysis: silent sources, sudden drops and seasonal expectation
  License:   BUSL-1.1
  Copyright: (c) 2026 HYPERI PTY LIMITED
-->

# Throughput analysis

DFE/XDR 2.0 and 2.1 told you when a source went quiet or fell off a cliff. 2.2 doesn't yet. This page describes the analysis as it comes back in 2.2, judged against what's normal for that time and day, with the calendar taken into account.

Status: design. Nothing on this page ships in 2.2.3. Tracked in <https://github.com/hyperi-io/dfe-engine/issues/622>.

## What it flags

A source is silent when it has sent nothing for longer than its window. It has dropped when its rate falls well below its recent rate. Both read 2.2's self-monitoring: the receiver and loader counters that land in the otel ClickHouse database on every deployment, the same rates the Components page reads (`src/dfe_engine/appmgmt/queries.yaml`). No new collector, and no data-path change.

Seasonal expectation goes further. A Monday at 09:00 and a Sunday at 03:00 are different normals, so the expectation is the inbound rate for a source at a given time on a given day of the week, and the deviation is how far the actual sits from it. That part is a general-purpose Python package, Apache-2.0, so it can stand on its own. DFE is its first consumer.

Without calendar exceptions it cries wolf every long weekend, and then nobody trusts it. A public holiday is compared against holidays, not a normal Monday. Change freezes, site shutdowns and known outage windows count too. Exceptions are data, a calendar per org or site with holiday defaults by country and region, never code.

## What you see

- OTel metrics for expected throughput, actual throughput and the deviation, per source.
- OTel alerts for a silent source, a sudden drop and a seasonal deviation, each with a configurable window.
- A shipped dashboard: the expected band against actual per source, the sources that are silent now, and the exceptions in force.

It's done when a source that stops sending, or drops well below its seasonal norm, raises an alert within its window, a holiday in the calendar stops that alert on the day, and the expected-against-actual view is on a shipped dashboard.
