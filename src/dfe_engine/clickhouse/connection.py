#  Project:      dfe-engine
#  File:         clickhouse/connection.py
#  Purpose:      Resolved connection identity + pooled-client cache (multi-target)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""ClickHouse connection identity + a pooled-client cache keyed on that identity.

The cache fixes the first-config-wins singleton footgun: the old
``ClickHouseManager`` kept the FIRST ``target_config_data`` it ever saw and
silently ignored later differing configs (binding the whole process to one
target). Here a client is keyed on the FULLY-RESOLVED connection identity -
``(Target, Profile)`` - so a second, different target (or the same target on a
different settings profile) gets its OWN pooled client instead of colliding.

Pattern from Snuba's ``ConnectionCache`` (FSL - idea, reimplemented): key on the
resolved node/creds/db/settings/driver, never on the logical inputs. One shared
urllib3 ``PoolManager`` backs every client (it pools per host internally); the
per-profile ``send_receive_timeout`` + baseline settings live on the client.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Any

import clickhouse_connect
from clickhouse_connect.driver import Client, httputil
from scalo.logger import logger

from .profiles import Profile


@dataclass(frozen=True, slots=True)
class Target:
    """A resolved ClickHouse connection identity - the cache key + build inputs.

    Frozen + hashable so it can key the connection cache. The password is part of
    the identity (a creds change must yield a fresh client) but is never logged -
    use :meth:`redacted` for log lines.
    """

    host: str
    port: int
    username: str | None
    password: str | None
    database: str | None
    secure: bool
    verify: bool

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> Target:
        """Build a Target from the canonical ``get_clickhouse_config`` dict.

        (``ch_host``/``ch_port``/``ch_username``/``ch_password``/``ch_database``/
        ``ch_secure``/``ch_verify`` - the one config shape every call site uses.)
        """
        return cls(
            host=cfg.get("ch_host", "localhost"),
            port=int(cfg.get("ch_port", 8123)),
            username=cfg.get("ch_username"),
            password=cfg.get("ch_password"),
            database=cfg.get("ch_database"),
            secure=bool(cfg.get("ch_secure", True)),
            verify=bool(cfg.get("ch_verify", False)),
        )

    def redacted(self) -> dict[str, Any]:
        """Identity for logging - password replaced by a presence flag."""
        return {
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "database": self.database,
            "secure": self.secure,
            "password_set": self.password is not None,
        }


class ConnectionCache:
    """Builds + caches pooled clickhouse-connect clients keyed on ``(Target, Profile)``.

    Thread-safe. One urllib3 ``PoolManager`` is shared by every client (it keys
    connections by host internally). ``evict`` drops a specific client (the
    resilience reconnect path calls it so the next acquire rebuilds a fresh one);
    ``clear`` tears everything down.
    """

    def __init__(self, *, connections_max: int = 300, num_pools: int = 10) -> None:
        self._lock = Lock()
        self._clients: dict[tuple[Target, Profile], Client] = {}
        self._pool_manager: Any = None
        self._connections_max = connections_max
        self._num_pools = num_pools

    def get_client(self, target: Target, profile: Profile = Profile.QUERY) -> Client:
        """Return the pooled client for ``(target, profile)``, building on first use."""
        key = (target, profile)
        client = self._clients.get(key)
        if client is not None:
            return client
        with self._lock:
            client = self._clients.get(key)
            if client is None:
                client = self._build(target, profile)
                self._clients[key] = client
            return client

    def evict(self, target: Target, profile: Profile) -> None:
        """Drop + close the client for ``(target, profile)`` (reconnect path)."""
        with self._lock:
            client = self._clients.pop((target, profile), None)
        if client is not None:
            try:
                client.close()
            except Exception:  # a close failure must not mask the outage
                pass

    def evict_target(self, target: Target) -> None:
        """Drop + close every profile's client for ``target`` (full reconnect)."""
        with self._lock:
            keys = [k for k in self._clients if k[0] == target]
            clients = [self._clients.pop(k) for k in keys]
        for client in clients:
            try:
                client.close()
            except Exception:  # best-effort cleanup - never mask the caller's error
                pass

    def clear(self) -> None:
        """Tear down all cached clients + the shared pool (teardown / tests)."""
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
            pool = self._pool_manager
            self._pool_manager = None
        for client in clients:
            try:
                client.close()
            except Exception:  # best-effort cleanup - never mask the caller's error
                pass
        if pool is not None:
            try:
                pool.clear()
            except Exception:  # best-effort cleanup - never mask the caller's error
                pass

    # -- internals ---------------------------------------------------

    def _pool(self) -> Any:
        if self._pool_manager is None:
            self._pool_manager = httputil.get_pool_manager(
                maxsize=self._connections_max, num_pools=self._num_pools
            )
        return self._pool_manager

    def _build(self, target: Target, profile: Profile) -> Client:
        spec = profile.spec
        params: dict[str, Any] = {
            "host": target.host,
            "port": target.port,
            "pool_mgr": self._pool(),
        }
        if target.username is not None:
            params["username"] = target.username
        if target.password is not None:
            params["password"] = target.password
        if target.database is not None:
            params["database"] = target.database
        if target.secure:
            params["secure"] = True
            params["verify"] = target.verify
        if spec.timeout_s is not None:
            params["send_receive_timeout"] = spec.timeout_s
        if spec.settings:
            params["settings"] = dict(spec.settings)

        logger.info(
            "Building ClickHouse client",
            profile=profile.value,
            **target.redacted(),
        )
        return clickhouse_connect.get_client(**params)
