#  Project:      dfe-engine
#  File:         keda_shim/shim.py
#  Purpose:      Config-driven ClickHouse-query -> KEDA metrics-api adapter (fail-safe)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The QueryShim - runs a config-defined ClickHouse query, returns one integer.

KEDA's ``metrics-api`` scaler polls the shim; the shim runs the SQL named in its
config catalogue and hands back a single number. The reason for a shim (rather than
KEDA talking to ClickHouse directly) is the FAIL-SAFE: a wrong or unreachable metric
must never run replicas UP and blow the cloud bill. So every query caches its
last-good value and, on ANY failure, returns that cache -> KEDA sees no change ->
scaling FREEZES at current. A cold start with no cache falls back to the query's
``cold_hold`` (0 = hold at min), so a broken metric can only ever hold or scale
DOWN, never up.

Queries live in config (``queries.yaml`` + an optional mounted override), not code,
so a HyperDX schema rename or a new scaling signal is a config edit, not a rebuild.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import clickhouse_connect
from scalo.logger import logger

from dfe_engine import scaling_pressure
from dfe_engine.settings import DFESettings
from dfe_engine.yaml_utils import deep_merge, yaml_load

_BUILTIN_QUERIES = Path(__file__).parent / "queries.yaml"
# Database and request-param values land in SQL IDENTIFIER position (a db name) or a
# bind slot, never as free HTML/text - so allow a safe identifier charset and reject
# anything else BEFORE it reaches ClickHouse. Belt to the CH bind braces.
_IDENT = re.compile(r"^[A-Za-z0-9_-]+$")


def _ch_client(settings: DFESettings) -> Any:
    """A raw clickhouse-connect client from the engine's CH settings.

    Raw (not the ClickHouseManager resilience wrapper): the shim is a tiny read-only
    poller, so it wants the plain client, not the autowake/pool machinery. Same
    host/port/username/password/secure/verify the hunt-runner and the integration
    ``ch_client`` fixture build from.
    """
    ch = settings.clickhouse
    return clickhouse_connect.get_client(
        host=ch.host,
        port=ch.port,
        username=ch.username,
        password=ch.password,
        secure=ch.secure,
        verify=ch.verify,
    )


class QueryShim:
    """Runs named, config-defined ClickHouse queries with a last-good fail-safe."""

    def __init__(
        self,
        settings: DFESettings,
        *,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._settings = settings
        self._client_factory = client_factory or (lambda: _ch_client(settings))
        self._client: Any = None
        self._cache: dict[str, int] = {}
        self._queries = self._load_queries()

    def _load_queries(self) -> dict[str, dict]:
        catalogue = yaml_load(_BUILTIN_QUERIES) or {}
        override = self._settings.keda_shim.query_config
        if override and Path(override).exists():
            catalogue = deep_merge(catalogue, yaml_load(Path(override)) or {})
        return catalogue.get("queries", {})

    @property
    def query_names(self) -> list[str]:
        """The query names this shim can answer (built-ins + any config override)."""
        return sorted(self._queries)

    def _client_get(self) -> Any:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def _db(self, token: str) -> str:
        """Resolve a query's ``database`` token to a validated CH identifier.

        ``otel`` and ``data`` both resolve to the one DFE database -- the tokens
        survive because the query catalogue names them, not because the tables
        live apart. A literal passes through for a query pointed elsewhere.
        """
        dfe = self._settings.clickhouse.effective_data_database
        resolved = {"otel": dfe, "data": dfe}.get(token, token)
        if not _IDENT.match(resolved):
            raise ValueError(f"unsafe database identifier: {resolved!r}")
        return resolved

    def _build(self, cfg: dict, params: dict[str, str]) -> tuple[str, dict[str, Any]]:
        """Return ``(sql, ch_bind_parameters)`` for a query config + request params."""
        db = self._db(str(cfg.get("database", "data")))
        if cfg.get("builtin") == "hunt_backlog":
            # ONE SQL source of truth for the due-count (no drift vs a copied string).
            from dfe_engine.hunt_runner.schedule import due_query

            return due_query(db), {}
        sql = scaling_pressure.apply(str(cfg["sql"]).replace("__DB__", db))
        ch_params: dict[str, Any] = dict(cfg.get("binds", {}))
        for name in cfg.get("params", []):
            value = params.get(name)
            if value is None:
                raise ValueError(f"missing required query param: {name}")
            if not _IDENT.match(value):
                raise ValueError(f"unsafe query param {name}={value!r}")
            ch_params[name] = value
        return sql, ch_params

    @staticmethod
    def _clamp(value: int, cfg: dict) -> int:
        lo, hi = cfg.get("clamp_min"), cfg.get("clamp_max")
        if lo is not None:
            value = max(int(lo), value)
        if hi is not None:
            value = min(int(hi), value)
        return value

    @staticmethod
    def _cache_key(name: str, params: dict[str, str]) -> str:
        return name + "|" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))

    def run(self, name: str, params: dict[str, str] | None = None) -> int:
        """Run query ``name`` and return one integer - NEVER raises to the caller.

        Unknown ``name`` is a config error (raised) so the route can log it; every
        RUNTIME failure is swallowed by the fail-safe and returns the last-good or
        cold-hold value, so KEDA can only ever hold or scale DOWN on a bad metric.
        """
        params = params or {}
        cfg = self._queries.get(name)
        if cfg is None:
            raise KeyError(name)
        cache_key = self._cache_key(name, params)
        cold_hold = int(cfg.get("cold_hold", 0))
        try:
            sql, ch_params = self._build(cfg, params)
            timeout = int(cfg.get("timeout_seconds", 5))
            result = self._client_get().query(
                sql, parameters=ch_params, settings={"max_execution_time": timeout}
            )
            rows = result.result_rows
            raw = rows[0][int(cfg.get("result_field", 0))] if rows else 0
            value = self._clamp(int(raw), cfg)
            self._cache[cache_key] = value
            return value
        except Exception as exc:  # fail-safe: a metric failure must NEVER reach KEDA
            # Drop a possibly-broken client so the next poll rebuilds it. Last-good
            # freezes scaling at current; no last-good holds at the conservative
            # cold_hold (min). Either way, a broken metric never scales UP.
            self._client = None
            cached = self._cache.get(cache_key)
            held = cached if cached is not None else cold_hold
            logger.warning(
                "keda-shim query failed; holding last-good/cold value (no scale-up)",
                query=name,
                params=params,
                held=held,
                had_cache=cached is not None,
                error=str(exc),
            )
            return held
