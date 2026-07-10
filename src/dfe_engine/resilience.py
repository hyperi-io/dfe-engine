#  Project:      dfe-engine
#  File:         resilience.py
#  Purpose:      dfe resilience primitives -- thin re-export of scalo.resilience
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""dfe resilience primitives - ONE import site for both shapes, all from scalo.

``scalo.resilience`` is the SSoT for the two complementary ways a dfe consumer
survives a flaky dependency, so this module DEFINES nothing - it is a thin
re-export so every consumer reaches for the same ``dfe_engine.resilience``
vocabulary:

- **reconnect-and-retry** (:class:`ReconnectingResilience`, with
  :class:`OutageState` / :class:`ResilienceConfig` / :class:`ServiceUnavailable`)
  - for a POOLED connection you can rebuild: a transient outage backs off,
  reconnects, and recovers on the next success. ClickHouse binds it (see
  ``dfe_engine.clickhouse.resilience``), injecting the CH error classifiers.
- **trip-and-reject** (:class:`CircuitBreaker`, with
  :class:`CircuitBreakerConfig` / :class:`CircuitBreakerError` /
  :class:`CircuitState`) - for a one-shot outbound call you should STOP hammering
  once it is clearly down: after N failures the circuit opens and fast-rejects,
  then half-opens to probe and closes on recovery.

``ReconnectingResilience`` and friends were dfe-local until scalo 2.29.6 promoted
them into ``scalo.resilience`` (a sibling of ``CircuitBreaker``); this module now
just re-exports scalo's, so the promotion is invisible to every caller that
imports from ``dfe_engine.resilience``.
"""

from __future__ import annotations

from scalo.resilience import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerError,
    CircuitState,
    OutageState,
    ReconnectingResilience,
    ResilienceConfig,
    ServiceUnavailable,
)

__all__ = [
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitBreakerError",
    "CircuitState",
    "OutageState",
    "ReconnectingResilience",
    "ResilienceConfig",
    "ServiceUnavailable",
]
