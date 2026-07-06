#  Project:      dfe-engine
#  File:         clickhouse_manager.py
#  Purpose:      ClickHouse access facade - cache + resilience + engine resolution
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Canonical ClickHouse access object.

Every ClickHouse interaction in the engine goes through this one object so
behaviour is consistent and sprawl is eliminated. It owns connection acquisition
(via :class:`~dfe_engine.clickhouse.connection.ConnectionCache`, keyed on the
resolved ``(Target, Profile)`` - which retires the first-config-wins singleton
footgun), resilience (:class:`~dfe_engine.clickhouse.resilience.ChResilience`:
reconnecting back-off on a CONNECTION outage + CH Cloud idle-wake), and
engine resolution (:class:`~dfe_engine.clickhouse.engines.EngineResolver`).

Acquire a client through :meth:`ClickHouseManager.get_clickhouse_client`; the
returned :class:`ClickHouseClientWrapper` exposes the execution verbs
(``query`` / ``command`` / ``query_rows`` / ``insert`` / ``execute``) plus
engine resolution. Nothing else in the engine imports ``clickhouse_connect``
directly (the enforcement guard fails CI on it).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from threading import Lock
from typing import Annotated, Any

from clickhouse_connect.driver import Client
from scalo.logger import logger

from ..settings import get_clickhouse_config, get_settings, is_dev_posture
from .connection import ConnectionCache, Target
from .engines import EngineResolver, ResolvedEngine
from .profiles import Profile
from .resilience import ChResilience, OutageState, ResilienceConfig

# One process-wide connection cache backs every manager (it keys clients by the
# resolved (Target, Profile), so two managers to different targets never collide).
_cache: ConnectionCache | None = None
_cache_lock = Lock()


def _connection_cache() -> ConnectionCache:
    """Return the process-wide connection cache, building it on first use."""
    global _cache
    if _cache is None:
        with _cache_lock:
            if _cache is None:
                _cache = ConnectionCache(connections_max=get_settings().clickhouse.connections_max)
    return _cache


class ClickHouseClientWrapper:
    """Execution facade over a ``(ClickHouseManager, Profile)`` pair.

    Every verb routes through the manager's resilience layer - a transient CH
    outage retries with reconnecting back-off, transparently to the caller. The
    wrapper is bound to a settings :class:`Profile` (the pool + baseline settings
    it reads through); :meth:`with_profile` returns a sibling on another profile,
    and writes go on ``Profile.INSERT`` regardless of the read profile.
    """

    def __init__(self, manager: ClickHouseManager, profile: Profile = Profile.INTERNAL) -> None:
        self._manager = manager
        self._profile = profile

    def __enter__(self) -> ClickHouseClientWrapper:
        # The manager + cache own the client lifecycle; `with get_clickhouse_client()
        # as ch` is scoping sugar only, so __exit__ must NOT close a shared client.
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    @property
    def _client(self) -> Client:
        """The raw driver client for this wrapper's profile (duck-typed callers)."""
        return self._manager.raw_client(self._profile)

    def with_profile(self, profile: Profile) -> ClickHouseClientWrapper:
        """Return a sibling wrapper bound to a different settings profile."""
        return ClickHouseClientWrapper(self._manager, profile)

    def _run(
        self,
        op: Callable[[Client], Any],
        operation: str = "query",
        profile: Profile | None = None,
    ) -> Any:
        """Route ``op`` through resilience, recording per-query observability.

        Times the call and emits a structured log event + (opt-in) metrics via
        ``clickhouse/metrics.py`` - labelled by profile / operation / outcome only
        (bounded cardinality). ``profile`` defaults to the wrapper's; writes pass
        ``Profile.INSERT`` so the one timed path serves every verb.
        """
        from .metrics import record_query

        prof = profile or self._profile
        start = time.perf_counter()
        outcome = "ok"
        try:
            return self._manager.run_resilient(op, prof)
        except Exception:
            outcome = "error"
            raise
        finally:
            record_query(
                profile=prof.value,
                operation=operation,
                outcome=outcome,
                duration_s=time.perf_counter() - start,
            )

    def _tagged(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Prepare a call's settings: attribution ``log_comment`` + kill-switch clamp.

        Merges the ambient :class:`DfeQueryTags` into ``settings["log_comment"]``,
        then clamps the resource ceilings per the live kill-switch severity (a
        no-op unless the incident brake is engaged and this profile is non-exempt).
        """
        from .attribution import merge_log_comment
        from .kill_switch import clamp_for_profile

        kwargs = dict(kwargs)
        kwargs["settings"] = clamp_for_profile(
            merge_log_comment(kwargs.get("settings")), self._profile
        )
        return kwargs

    def query(self, query: str, *args: Any, **kwargs: Any) -> Any:
        """Run a SELECT, returning the clickhouse-connect QueryResult (attribution-tagged)."""
        kwargs = self._tagged(kwargs)
        return self._run(lambda c: c.query(query, *args, **kwargs))

    def command(self, statement: str, *args: Any, **kwargs: Any) -> Any:
        """Run a DDL/DML statement (clickhouse-connect ``command`` passthrough; tagged)."""
        kwargs = self._tagged(kwargs)
        return self._run(lambda c: c.command(statement, *args, **kwargs), operation="command")

    def query_rows(self, query: str, *args: Any, **kwargs: Any) -> tuple[list[str], list]:
        """Run a SELECT and return ``(column_names, result_rows)`` (attribution-tagged)."""
        kwargs = self._tagged(kwargs)
        result = self._run(lambda c: c.query(query, *args, **kwargs))
        return list(result.column_names), result.result_rows

    def insert(
        self,
        table: str,
        data: list,
        *,
        column_names: list[str],
        database: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> Any:
        """Bulk-insert rows - the first-class write path.

        Replaces the old brittle regex re-parser (callers no longer reach past the
        wrapper for a raw client). Writes run on ``Profile.INSERT``.

        Args:
            table: destination table name.
            data: row tuples, aligned to ``column_names``.
            column_names: the columns ``data`` supplies, in order.
            database: qualifying database, or None to use the client default.
            settings: optional per-insert ClickHouse settings.
        """

        def _op(c: Client) -> Any:
            from .attribution import merge_log_comment
            from .kill_switch import clamp_for_profile

            kwargs: dict[str, Any] = {
                "column_names": column_names,
                # INSERT is a kill-switch-exempt profile (the clamp early-returns on
                # the profile check), but route through it for one settings-prep path.
                "settings": clamp_for_profile(merge_log_comment(settings), Profile.INSERT),
            }
            if database is not None:
                kwargs["database"] = database
            return c.insert(table, data, **kwargs)

        return self._run(_op, operation="insert", profile=Profile.INSERT)

    def resolve_engine(self, spec: Any, database: str) -> ResolvedEngine:
        """Resolve the storage engine for ``database`` by sensing the live server."""
        return self._manager.engine_resolver().resolve(spec, database)

    def sensed_topology(self, database: str) -> str:
        """The sensed topology ("single"|"replicated") for feeding a DDLConfig."""
        return self._manager.engine_resolver().sensed_topology(database)

    def execute(self, query: str, *args: Any, **kwargs: Any) -> Any:
        """Route a statement to command() or query() by type (compat helper).

        For clickhouse-driver-style code. Splits multi-statement queries on ``;``.
        INSERT-with-data is not supported here - use :meth:`insert`.
        """
        query = query.strip().rstrip(";").strip()
        if ";" in query:
            statements = [s.strip() for s in query.split(";") if s.strip()]
            result: list = []
            for stmt in statements:
                result += self._execute_single(stmt, *args, **kwargs)
            return result
        return self._execute_single(query, *args, **kwargs)

    def _execute_single(self, query: str, *args: Any, **kwargs: Any) -> Any:
        query_upper = query.strip().upper()
        kwargs = self._tagged(kwargs)
        if query_upper.startswith(("DESCRIBE", "DESC", "EXISTS", "EXPLAIN", "SHOW")):
            return self._run(lambda c: c.query(query, *args, **kwargs).result_rows)
        if query_upper.startswith("INSERT") and args and isinstance(args[0], (list, tuple)):
            raise ValueError("INSERT with data must use ClickHouseClientWrapper.insert()")
        if query_upper.startswith(
            (
                "CREATE",
                "DROP",
                "ALTER",
                "TRUNCATE",
                "RENAME",
                "INSERT",
                "DELETE",
                "UPDATE",
                "SET",
                "USE",
                "GRANT",
                "REVOKE",
                "ATTACH",
                "DETACH",
                "OPTIMIZE",
                "EXCHANGE",
                "SYSTEM",
                "CHECK",
                "KILL",
            )
        ):
            return self._run(lambda c: c.command(query, *args, **kwargs), operation="command")
        return self._run(lambda c: c.query(query, *args, **kwargs).result_rows)


class ClickHouseManager:
    """Per-target handle over the shared connection cache + resilience layer.

    Managers are registered by their resolved :class:`Target`, so a differing
    config yields its OWN manager (no first-call-wins). Acquire clients via
    :meth:`get_clickhouse_client`.
    """

    _instances: dict[Target, ClickHouseManager] = {}
    _reg_lock = Lock()

    def __init__(self, target_config_data: dict | None = None) -> None:
        cfg = target_config_data or get_clickhouse_config(get_settings())
        self.target_config_data = cfg
        self._target = Target.from_config(cfg)
        settings = get_settings()
        self.connections_max = settings.clickhouse.connections_max

        # Resilience (Phase 0) + opt-in CH Cloud auto-wake (Phase 2).
        rs = settings.clickhouse.resilience
        self._cloud_settings = settings.clickhouse.cloud
        self._env = settings.env
        self._autowake_lock = Lock()
        self._autowake_inflight = False
        self._resilience = ChResilience(
            ResilienceConfig(
                enabled=rs.enabled,
                wait_initial=rs.wait_initial_seconds,
                wait_max=rs.wait_max_seconds,
                wait_multiplier=rs.wait_multiplier,
                budget_seconds=rs.budget_seconds,
                waking_budget_seconds=rs.waking_budget_seconds,
            ),
            reconnect=self._reconnect,
            on_connect_failure=self._maybe_autowake,
        )

    @classmethod
    def get_instance(cls, target_config_data: dict | None = None) -> ClickHouseManager:
        """Return the manager for ``target_config_data`` (settings-backed if None).

        Registered by resolved :class:`Target`: identical configs share a manager,
        a differing config gets its own (retires the first-config-wins footgun).
        """
        cfg = target_config_data or get_clickhouse_config(get_settings())
        target = Target.from_config(cfg)
        with cls._reg_lock:
            manager = cls._instances.get(target)
            if manager is None:
                manager = cls(cfg)
                cls._instances[target] = manager
            return manager

    @classmethod
    def reset_instance(cls) -> None:
        """Drop all registered managers + tear down the shared cache (tests)."""
        with cls._reg_lock:
            cls._instances.clear()
        if _cache is not None:
            _cache.clear()

    @property
    def resilience(self) -> ChResilience:
        """The connection-resilience layer (its ``.state`` drives CH readiness)."""
        return self._resilience

    def get_clickhouse_client(self, profile: Profile = Profile.INTERNAL) -> ClickHouseClientWrapper:
        """Return a client wrapper bound to this manager + ``profile``.

        Warms the pooled client eagerly (so a config/auth error surfaces here, not
        on first query), then returns the resilience-routed wrapper.
        """
        try:
            _connection_cache().get_client(self._target, profile)
            return ClickHouseClientWrapper(self, profile)
        except Exception as e:
            logger.error(f"An unexpected error occurred during client acquisition: {e}")
            raise

    def raw_client(self, profile: Profile = Profile.INTERNAL) -> Client:
        """The pooled driver client for ``(this target, profile)``."""
        return _connection_cache().get_client(self._target, profile)

    def engine_resolver(self) -> EngineResolver:
        """An engine resolver that senses via this target's INTERNAL client."""
        return EngineResolver(
            client=self.raw_client(Profile.INTERNAL),
            topology_setting=get_settings().clickhouse.topology,
        )

    def run_resilient(
        self, op: Callable[[Client], Any], profile: Profile = Profile.INTERNAL
    ) -> Any:
        """Execute ``op(client)`` with reconnecting back-off on a CONNECTION outage.

        On a connection outage the resilience layer evicts + rebuilds the pooled
        client and retries until success or the configured budget is exhausted
        (then :class:`ChUnavailable` -> the API maps it to 503). A query-level
        error (syntax/memory/auth) surfaces immediately.
        """
        result = self._resilience.run(
            lambda: op(_connection_cache().get_client(self._target, profile))
        )
        if self._autowake_inflight:
            self._autowake_inflight = False
        return result

    def ping(self) -> bool:
        """Lightweight readiness probe: True when a trivial query succeeds."""
        try:
            self.run_resilient(lambda c: c.command("SELECT 1"))
            return True
        except Exception:
            return False

    def _reconnect(self) -> None:
        """Evict this target's cached clients so the next op rebuilds them fresh.

        The resilience layer calls this between retry attempts to recover a bad
        pool; the next :meth:`run_resilient` rebuilds via the cache.
        """
        _connection_cache().evict_target(self._target)

    def _maybe_autowake(self) -> bool:
        """Phase 2 auto-wake hook: START a stopped CH Cloud service on a connect
        failure, when opted in. Returns True (-> WAKING, extended budget) if a wake
        is in flight; False for a plain transient outage.

        Cascade gate (all must hold): ``clickhouse.cloud.autowake`` on, the
        control-plane creds present, AND a NON-PRODUCTION posture (a billable
        start is a dev convenience, never automatic in prod). Concurrent connect
        failures dedup on the in-flight latch so only one start is issued.
        """
        cloud = self._cloud_settings
        if not cloud.autowake or not cloud.configured:
            return False
        if not is_dev_posture(self._env):
            logger.warning(
                "CH Cloud autowake ignored: billable auto-start is dev-only", env=self._env
            )
            return False

        with self._autowake_lock:
            try:
                from .cloud import CloudService

                svc = CloudService(cloud)
                status = svc.status()
                if status.is_running:
                    self._autowake_inflight = False
                    return False  # running -> a plain transient blip, not a wake
                if status.state == "starting" or self._autowake_inflight:
                    return True  # already waking -> keep the extended (cold-start) budget
                self._autowake_inflight = True
                logger.info(
                    "CH Cloud autowake: starting stopped service (billable)",
                    service=status.name,
                    from_state=status.state,
                )
                svc.start()
                return True
            except Exception as exc:
                logger.warning("CH Cloud autowake failed", error=str(exc))
                return False

    def teardown_test_databases(
        self, test_databases: Annotated[list[str], "min_length = 1"]
    ) -> None:
        """Drop the given test databases (test lifecycle helper)."""
        client = self.get_clickhouse_client()
        try:
            for db in test_databases:
                logger.info(f"Dropping database {db}")
                client.execute(f"DROP DATABASE IF EXISTS {db}")
            logger.info("All test databases dropped successfully.")
        except Exception as e:
            logger.error(f"Failed to drop test databases: {e}")


def clickhouse_outage_state() -> OutageState:
    """The primary (settings-backed) CH manager's outage state (readiness probe).

    HEALTHY when nothing has connected yet. Reads the live resilience state of the
    manager bound to the settings-derived target - the connection the app depends
    on - so the k8s readiness gate reflects a real outage / Cloud warm-up.
    """
    target = Target.from_config(get_clickhouse_config(get_settings()))
    manager = ClickHouseManager._instances.get(target)
    if manager is None:
        return OutageState.HEALTHY
    return manager.resilience.state


def get_pooled_client(config: dict, profile: Profile = Profile.QUERY) -> Client:
    """Return a pooled raw driver client for an arbitrary target config.

    The sanctioned way for a caller that genuinely needs the RAW clickhouse-connect
    client (restricted-user reads, per-connection fixed-user reads) to get one that
    shares the process pool - instead of a direct ``clickhouse_connect.get_client``
    bypass. Keyed on the resolved ``(Target, profile)``, so distinct creds/targets
    each get their own pooled client.
    """
    return _connection_cache().get_client(Target.from_config(config), profile)
