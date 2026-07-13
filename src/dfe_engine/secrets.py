#  Project:      dfe-engine
#  File:         secrets.py
#  Purpose:      The one seam for secrets the engine mints (scalo.secrets)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The single seam for secrets the engine MINTS - backend chosen by config.

The engine reads runtime config secrets from env (backend-agnostic: ESO/compose
fill the env). This module is the WRITE seam for secrets the engine generates
(per-group ClickHouse passwords, OIDC client secrets, API keys): they go to
``scalo.secrets``, provider selected by ``settings.secrets`` -  ``file`` /
``ansible_vault`` for dfe-docker (a local file, no extra service), ``openbao`` on
k8s (external-first), ``aws`` / ``gcp`` / ``azure`` for cloud (deferred).

The engine never imports a backend SDK - scalo owns that (see docs/deployment/backing-services.md).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scalo.secrets import SecretsManager
from scalo.secrets.exceptions import SecretAlreadyExistsError, SecretNotFoundError

from dfe_engine.settings import SecretsSettings

# Providers whose "path" is a real filesystem path - prefix with the configured root.
_ROOTED = {"file", "ansible_vault"}


class DfeSecrets:
    """Provider-agnostic accessor for secrets the engine mints.

    ``put`` upserts (minted secrets rotate); ``get`` returns the UTF-8 string;
    ``delete`` removes it. File-based providers store under the configured root;
    openbao/cloud use the logical path as the KV path.
    """

    def __init__(self, manager: SecretsManager, provider: str, root: str, mount: str = "") -> None:
        self._mgr = manager
        self._provider = provider
        self._root = root
        self._mount = mount

    def _full(self, path: str) -> str:
        if self._provider in _ROOTED:
            return str(Path(self._root) / path)
        if self._provider in ("openbao", "vault") and self._mount:
            # openbao KV path = <kv-mount>/<path> (the provider inserts /data/ for KV v2)
            return f"{self._mount}/{path}"
        return path

    def put(self, path: str, value: str) -> None:
        """Create or update a secret (upsert)."""
        full = self._full(path)
        data = value.encode("utf-8")
        try:
            self._mgr.create_sync(full, data, provider=self._provider)
        except SecretAlreadyExistsError:
            self._mgr.update_sync(full, data, provider=self._provider)
        # scalo keeps an in-memory read cache that writes do not invalidate;
        # clear it so a rotated secret is read fresh.
        self._mgr.clear_cache()

    def get(self, path: str) -> str:
        """Fetch a secret as a string. Raises SecretNotFoundError if absent."""
        # openbao KV v2 wraps the value under a "value" field; file stores it raw.
        key = "value" if self._provider in ("openbao", "vault") else None
        return self._mgr.get_sync(self._full(path), key=key, provider=self._provider).decode(
            "utf-8"
        )

    def exists(self, path: str) -> bool:
        try:
            self.get(path)
            return True
        except SecretNotFoundError:
            return False

    def delete(self, path: str) -> None:
        """Remove a secret (no error if it is already gone)."""
        try:
            self._mgr.delete_sync(self._full(path), provider=self._provider)
        except SecretNotFoundError:
            pass
        self._mgr.clear_cache()


def _to_scalo_config(cfg: SecretsSettings) -> dict[str, Any]:
    """Map DFE SecretsSettings -> scalo.secrets from_config dict (file needs none)."""
    # Minted secrets rotate, so no stale read cache.
    scfg: dict[str, Any] = {"cache": {"enabled": False}}
    if cfg.provider in ("openbao", "vault"):
        # AppRole in prod (role_id in config; the secret_id is itself a secret and
        # comes from the VAULT_SECRET_ID env, injected by ESO/infra - never config).
        # cfg.mount is the KV secrets mount (prefixes the path), NOT the auth mount.
        auth: dict[str, Any] = {"method": "approle" if cfg.role else "token"}
        if cfg.role:
            auth["role_id"] = cfg.role
        scfg["openbao"] = {"address": cfg.addr, "auth": auth}
    elif cfg.provider in ("aws", "gcp", "azure", "ansible_vault"):
        # ambient cloud creds / env-supplied ansible-vault password
        scfg[cfg.provider] = {}
    # "file" needs no config block (always available).
    return scfg


def build_secrets(cfg: SecretsSettings) -> DfeSecrets:
    """Build the DFE secrets accessor for the configured backend."""
    manager = SecretsManager.from_config(_to_scalo_config(cfg))
    return DfeSecrets(manager, provider=cfg.provider, root=cfg.path, mount=cfg.mount)
