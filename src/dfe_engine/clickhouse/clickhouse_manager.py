#  Project:      dfe-engine
#  File:         clickhouse_manager.py
#  Purpose:      ClickHouse connection management using clickhouse-connect
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2025 HYPERI PTY LIMITED

"""
ClickHouse connection management using clickhouse-connect.

Uses the official ClickHouse Inc. driver with built-in HTTP connection pooling.

Every data-plane op runs through scalo's
:class:`~scalo.resilience.ReconnectingResilience`, which the manager binds to
ClickHouse: a CONNECTION outage (transport error or a CH connection code) backs
off AND rebuilds the pooled client between attempts; a RATE_LIMITED (202) error
backs off WITHOUT reconnecting (the connection is fine, the server is busy); a
genuine query error (syntax / memory / access / exec-timeout) surfaces
immediately un-retried. When ``clickhouse.cloud.autowake`` is on, a connection
outage additionally fires the CH Cloud resume hook (START a stopped/idle
service) and extends the budget to the cold-start window. The CH error
classifiers live in :mod:`dfe_engine.clickhouse.errors`; the generic engine lives
in scalo (promoted from dfe in scalo 2.29.6). Budget exhaustion raises scalo's
:class:`~scalo.resilience.ServiceUnavailable`, which the API maps to 503.
"""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from typing import TYPE_CHECKING, Annotated, Any, TypeVar

import clickhouse_connect
from clickhouse_connect.driver import Client, httputil
from scalo.logger import logger
from scalo.resilience import ReconnectingResilience, ResilienceConfig, ServiceUnavailable

from ..settings import get_settings
from .attribution import merge_log_comment
from .errors import is_connection_error, is_retryable_error
from .tls import resolve_clickhouse_tls

if TYPE_CHECKING:
    from ..settings import DFESettings

T = TypeVar("T")


class ClickHouseClientWrapper:
    """
    Wrapper around clickhouse-connect Client to provide backward-compatible execute() method.

    clickhouse-connect uses:
    - command() for DDL/DML statements (CREATE, DROP, ALTER, INSERT without data)
    - query() for SELECT statements that return data

    This wrapper provides execute() that auto-routes to the appropriate method, and
    passes query()/command()/insert() straight through, so a caller written against
    clickhouse-connect (the hunt coordinator) works on it unchanged.

    Every op runs through the manager's resilience layer
    (:meth:`ClickHouseManager.run_resilient`), so a transient CH outage reconnects
    and recovers transparently; the ``_client`` property always resolves the LIVE
    pooled client, so an op re-run after a reconnect uses the rebuilt one and
    external admin paths (RBAC reconcile, the sampler unwrap) that reach for the
    raw driver client still work.
    """

    def __init__(self, manager: ClickHouseManager):
        self._manager = manager

    def __enter__(self) -> ClickHouseClientWrapper:
        # The ClickHouseManager singleton owns the underlying client's lifecycle;
        # `with get_clickhouse_client() as ch` is scoping sugar only, so __exit__
        # must NOT close the shared client out from under other callers.
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    @property
    def _client(self) -> Client:
        """The LIVE clickhouse-connect client (lazily built; rebuilt after a
        reconnect).

        A property, not a stored reference, so (a) an op re-run inside the
        resilience loop after ``reconnect`` picks up the freshly rebuilt pooled
        client, and (b) the admin paths that reach for the raw driver client
        (``.get_clickhouse_client()._client`` for the RBAC reconcile; the sampler's
        ``_raw_client`` unwrap) keep resolving.
        """
        return self._manager._live_client()

    def query(self, query: str, *args, **kwargs):
        """Raw SELECT -> the clickhouse-connect QueryResult (``.column_names`` /
        ``.result_rows``). Use when the caller needs the result object;
        ``execute()`` yields only result_rows and ``query_rows()`` returns
        ``(columns, rows)``.
        """
        kwargs["settings"] = merge_log_comment(kwargs.get("settings"))
        return self._manager.run_resilient(lambda: self._client.query(query, *args, **kwargs))

    def command(self, statement: str, *args, **kwargs):
        """Raw DDL/DML passthrough to clickhouse-connect's ``command()``."""
        kwargs["settings"] = merge_log_comment(kwargs.get("settings"))
        return self._manager.run_resilient(lambda: self._client.command(statement, *args, **kwargs))

    def insert(self, table: str, *args, **kwargs):
        """Row insert -> clickhouse-connect's ``insert(table, rows, column_names=,
        database=)``, through the resilience layer.

        A retry can insert the same row twice, which is safe only where the table
        collapses duplicates; every hunt coordination table this is used for is
        ReplacingMergeTree.
        """
        kwargs["settings"] = merge_log_comment(kwargs.get("settings"))
        return self._manager.run_resilient(lambda: self._client.insert(table, *args, **kwargs))

    def query_rows(self, query: str, *args, **kwargs):
        """Run a SELECT and return ``(column_names, result_rows)``.

        ``execute()`` discards column names (it only yields ``result_rows``),
        which is fine for scalar/aggregate reads but loses the shape needed to
        assemble row dicts. Use this when the caller needs to map values back to
        their columns (e.g. sampling whole rows).
        """

        kwargs["settings"] = merge_log_comment(kwargs.get("settings"))

        def op():
            result = self._client.query(query, *args, **kwargs)
            return list(result.column_names), result.result_rows

        return self._manager.run_resilient(op)

    def execute(self, query: str, *args, **kwargs):
        """
        Execute a query, routing to command() or query() based on query type.

        For backward compatibility with clickhouse-driver style code.
        Handles multi-statement queries by splitting on semicolons.
        """
        # Strip trailing semicolons and whitespace
        query = query.strip().rstrip(";").strip()

        # Check if this is a multi-statement query
        # Simple heuristic: if there's a semicolon not inside quotes, split.
        # Each statement is its OWN resilient unit (via _execute_single), so a
        # reconnect mid-batch re-runs only the failing statement, never the ones
        # already applied.
        if ";" in query:
            # Split and execute each statement
            statements = [s.strip() for s in query.split(";") if s.strip()]
            result = []
            for stmt in statements:
                result += self._execute_single(stmt, *args, **kwargs)
            return result

        return self._execute_single(query, *args, **kwargs)

    def _execute_single(self, query: str, *args, **kwargs):
        """Execute a single query statement through the resilience layer."""
        query_upper = query.strip().upper()
        # Attribution: stamp the log_comment tag here too - the DESCRIBE/DDL/SELECT
        # sub-paths below call self._client directly, bypassing the query/command/
        # query_rows wrapper hooks, so execute()'d DDL would otherwise be unattributed.
        kwargs["settings"] = merge_log_comment(kwargs.get("settings"))

        # DESCRIBE, DESC, EXISTS, EXPLAIN, SHOW return data - use query()
        if query_upper.startswith(("DESCRIBE", "DESC", "EXISTS", "EXPLAIN", "SHOW")):
            return self._manager.run_resilient(
                lambda: self._client.query(query, *args, **kwargs).result_rows
            )

        # INSERT with data - use insert() method
        # clickhouse-driver style: execute("INSERT INTO table (cols) VALUES", [(data, ...)])
        if query_upper.startswith("INSERT") and args and isinstance(args[0], (list, tuple)):
            data = args[0]
            return self._manager.run_resilient(lambda: self._handle_insert_with_data(query, data))

        # DDL/DML commands that don't return data go to command()
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
            return self._manager.run_resilient(lambda: self._client.command(query, *args, **kwargs))

        # SELECT queries return data
        return self._manager.run_resilient(
            lambda: self._client.query(query, *args, **kwargs).result_rows
        )

    def _handle_insert_with_data(self, query: str, data: list):
        """Handle INSERT statements with data using clickhouse-connect's insert() method.

        Converts clickhouse-driver style INSERT calls to clickhouse-connect format.
        """
        import re

        # Parse INSERT INTO table (columns) VALUES
        # Pattern: INSERT INTO [db.]table (col1, col2, ...) VALUES
        pattern = r"INSERT\s+INTO\s+([^\s(]+)\s*\(\s*([^)]+)\s*\)\s*VALUES"
        match = re.search(pattern, query, re.IGNORECASE)

        if not match:
            # Fallback: try to execute as raw query (may fail)
            raise ValueError(f"Cannot parse INSERT query for data insertion: {query[:100]}...")

        table_name = match.group(1)
        columns_str = match.group(2)
        column_names = [c.strip() for c in columns_str.split(",")]

        # Use clickhouse-connect's insert() method
        return self._client.insert(table_name, data, column_names=column_names)


class ClickHouseManager:
    """
    Manages ClickHouse connections using clickhouse-connect.

    Uses built-in HTTP connection pooling via urllib3.
    Pool configuration is managed via settings.clickhouse.connections_max.

    Owns the scalo :class:`~scalo.resilience.ReconnectingResilience` that every
    data-plane op runs through (see :meth:`run_resilient`): it injects the CH
    error classifiers, the reconnect (rebuild the pooled client) and - when
    ``clickhouse.cloud.autowake`` is on - the CH Cloud resume hook.
    """

    _instance = None  # Singleton instance

    def __init__(
        self,
        target_config_data: dict | None = None,
        *,
        settings: DFESettings | None = None,
        sleep: Callable[[float], None] | None = None,
        now: Callable[[], float] | None = None,
        cloud_service: Any | None = None,
    ):
        self.lock = Lock()
        self.target_config_data = target_config_data or {}
        self._settings = settings or get_settings()
        self.connections_max = self._settings.clickhouse.connections_max
        self._client: Client | None = None
        self._pool_manager = None
        # Reconnect-and-retry engine (scalo), built lazily on first op so the
        # classifiers + config are read once the settings are in place.
        self._resilience: ReconnectingResilience | None = None
        # Injectable clock -> the resilience layer (tests pass fakes so they never
        # really sleep and never race a wall clock).
        self._sleep = sleep
        self._now = now
        # Injectable CH Cloud lifecycle seam for the autowake resume hook. Tests
        # inject a DOUBLE so no paid Cloud instance is ever touched; production
        # builds one from the clickhouse.cloud control-plane settings.
        self._cloud_service = cloud_service

    @classmethod
    def get_instance(cls, target_config_data: dict | None = None):
        if cls._instance is None:
            cls._instance = cls(target_config_data)
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """Reset the singleton instance (useful for testing)."""
        if cls._instance is not None:
            cls._instance._cleanup()
            cls._instance = None

    def close(self) -> None:
        """Close this manager's pooled client and pool; the next op builds a fresh one."""
        self._cleanup()

    def get_clickhouse_client(self) -> ClickHouseClientWrapper:
        """Get a ClickHouse client wrapper with connection pooling + resilience.

        Returns a wrapper whose ops run through the resilience layer; the client
        itself is built lazily on first use (so an INITIAL connect to a stopped CH
        Cloud service is covered by the same reconnect/auto-wake path as a
        mid-session drop). Returns a wrapper that provides the backward-compatible
        execute() method.
        """
        return ClickHouseClientWrapper(self)

    def run_resilient(self, op: Callable[[], T]) -> T:
        """Run *op* through the reconnect-and-retry engine.

        A transient CH outage backs off and recovers on the next success (a
        connection outage also rebuilds the pooled client + may auto-wake); a
        genuine query error surfaces immediately; budget exhaustion raises
        :class:`~scalo.resilience.ServiceUnavailable` (API -> 503).
        """
        return self._get_resilience().run(op)

    def ping(self) -> bool:
        """Fast readiness probe: is ClickHouse reachable right now?

        A SINGLE bounded check (clickhouse-connect's ``/ping``, hard 3s
        timeout) that DELIBERATELY bypasses :meth:`run_resilient`. A readiness
        poll must fail fast and must NOT sit inside the reconnect/auto-wake
        budget: that budget is for real operations, a paused CH Cloud has to
        read as NOT ready, and a kubelet poll must never trigger a (billable)
        auto-wake. Never raises - any failure (unreachable, build error) reads
        as not-ready.
        """
        try:
            return bool(self._live_client().ping())
        except Exception:
            return False

    def _live_client(self) -> Client:
        """Return the live pooled client, building it if absent (or after a reconnect)."""
        if self._client is None:
            self._initialize_client()
        if self._client is None:  # _initialize_client sets it or raises
            raise RuntimeError("ClickHouse client failed to initialise")
        return self._client

    def _reconnect(self) -> None:
        """Resilience reconnect hook: tear the pooled client down so the next op
        rebuilds a fresh one against the recovered (or newly-woken) server."""
        self._cleanup()

    def _get_resilience(self) -> ReconnectingResilience:
        """Build (once) the CH-bound reconnect-and-retry engine from settings.

        Injects the CH error classifiers (connection vs rate-limit vs genuine
        query error), the reconnect, and - only when ``clickhouse.cloud.autowake``
        is on - the Cloud resume hook. Config is the ``clickhouse.resilience``
        block (config-cascade), passed straight into scalo's ``ResilienceConfig``.
        """
        if self._resilience is None:
            ch = self._settings.clickhouse
            config = ResilienceConfig(**ch.resilience.model_dump())
            clock: dict[str, Any] = {}
            if self._sleep is not None:
                clock["sleep"] = self._sleep
            if self._now is not None:
                clock["now"] = self._now
            self._resilience = ReconnectingResilience(
                config,
                name="ClickHouse",
                is_transient=is_retryable_error,
                is_reconnectable=is_connection_error,
                reconnect=self._reconnect,
                on_connect_failure=self._autowake_resume if ch.cloud.autowake else None,
                unavailable_exc=ServiceUnavailable,
                **clock,
            )
        return self._resilience

    def _autowake_resume(self) -> bool:
        """on_connect_failure hook (wired ONLY when clickhouse.cloud.autowake is on).

        A CH connection outage may be a stopped/idle CH Cloud service, so START it
        (billable) and report a wake so the resilience budget extends to the
        cold-start window; the reconnect loop then recovers the INSTANT the woken
        service accepts connections. Returns False (a plain transient outage) when
        the service is already running/starting or the wake could not be initiated
        - never lets a lifecycle error escape into the retry loop.
        """
        try:
            service = self._cloud_service_for_autowake()
            status = service.status()
            if status.is_running or status.state == "starting":
                return False
            service.start()  # idempotent + billable
            logger.info("CH Cloud autowake: resume issued, extending resilience budget")
            return True
        except Exception as exc:
            logger.warning("CH Cloud autowake resume failed", error=str(exc))
            return False

    def _cloud_service_for_autowake(self):
        """The CH Cloud lifecycle driver for the autowake hook.

        Tests inject a double via the constructor's ``cloud_service`` so no paid
        instance is touched; production builds one from the control-plane settings.
        """
        if self._cloud_service is not None:
            return self._cloud_service
        from .cloud import CloudService

        return CloudService(self._settings.clickhouse.cloud)

    def _initialize_client(self):
        """Initialize the ClickHouse client with connection pooling."""
        try:
            host = self.target_config_data.get("ch_host", "localhost")
            port = self.target_config_data.get("ch_port", 8123)
            user = self.target_config_data.get("ch_username")
            password = self.target_config_data.get("ch_password")
            secure = self.target_config_data.get("ch_secure", True)
            # verify defaults to None -> scalo's SCALO_TLS_VERIFY escape valve
            # (cert verification ON unless the whole environment disables it). An
            # explicit ch_verify (DFE_CLICKHOUSE_VERIFY) overrides per-CH.
            verify = self.target_config_data.get("ch_verify")
            ca_cert = self.target_config_data.get("ch_ca_cert")

            # The one TLS resolver every clickhouse-connect client in the engine
            # goes through: verify defaults ON, and an unreadable ca_cert refuses
            # here rather than silently connecting without a trust anchor.
            tls = resolve_clickhouse_tls(secure=secure, verify=verify, ca_cert=ca_cert)

            is_password_set = password is not None

            logger.info(
                "Initializing ClickHouse client",
                user=user,
                host=host,
                port=port,
                secure=secure,
                verify=tls.verify,
                password_set=is_password_set,
            )

            # Custom pool manager for connection pooling (clickhouse-connect uses
            # urllib3). TLS verification + CA are configured HERE so pooled
            # connections carry the posture; cert_reqs/ca_certs only bite on the
            # HTTPS (secure) path.
            self._pool_manager = httputil.get_pool_manager(
                maxsize=self.connections_max,
                num_pools=10,
                verify=tls.verify,
                ca_cert=tls.ca_cert,
            )

            # Build connection parameters. Typed dict[str, Any] because the values
            # are heterogeneous (str/int/PoolManager/bool) and get **-unpacked into
            # clickhouse_connect.get_client's precisely-typed kwargs.
            connect_params: dict[str, Any] = {
                "host": host,
                "port": port,
                "pool_mgr": self._pool_manager,
                # One client serves every worker thread, and the ClickHouse server
                # refuses a second query in flight on one session (SESSION_IS_LOCKED).
                "autogenerate_session_id": False,
            }

            # Add authentication if provided
            if user is not None:
                connect_params["username"] = user
            if password is not None:
                connect_params["password"] = password

            # Configure HTTPS. ca_cert is NOT repeated here -- it is already baked
            # into pool_mgr above, and clickhouse-connect ignores a ca_cert kwarg
            # once a pool_mgr is supplied.
            if tls.secure:
                connect_params["secure"] = True
                connect_params["verify"] = tls.verify

            if host == "localhost" and (user is not None or password is not None):
                logger.warning(
                    "Connecting to localhost with authentication. "
                    "Ensure your local cluster has proper auth configured."
                )

            # Log connection params (without password)
            log_params = {k: v for k, v in connect_params.items() if k != "password"}
            log_params.pop("pool_mgr", None)  # Don't log pool manager object
            logger.info(f"Creating clickhouse-connect client with: {log_params}")

            with self.lock:
                self._client = clickhouse_connect.get_client(**connect_params)

            logger.info("ClickHouse client initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize ClickHouse client: {e}")
            if host == "localhost":
                logger.warning(
                    "If using localhost, ensure ClickHouse is running and accessible. "
                    "For local dev without auth, do not set username/password in targets file."
                )
            raise

    def _cleanup(self):
        """Clean up the ClickHouse client and pool."""
        try:
            if self._client is not None:
                self._client.close()
                self._client = None
                logger.info("ClickHouse client closed successfully.")
            if self._pool_manager is not None:
                self._pool_manager.clear()
                self._pool_manager = None
                logger.info("ClickHouse connection pool cleared successfully.")
        except Exception as e:
            logger.error(f"Failed to cleanup ClickHouse client: {e}")

    def teardown_test_databases(self, test_databases: Annotated[list[str], "min_length = 1"]):
        """Drop test databases."""
        client = self.get_clickhouse_client()
        try:
            for db in test_databases:
                logger.info(f"Dropping database {db}")
                client.execute(f"DROP DATABASE IF EXISTS {db}")
            logger.info("All test databases dropped successfully.")
        except Exception as e:
            logger.error(f"Failed to drop test databases: {e}")
