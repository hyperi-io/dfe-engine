#  Project:      dfe-engine
#  File:         secrets.py
#  Purpose:      The one seam for secrets the engine mints (scalo.secrets)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The single seam for secrets the engine MINTS - backend chosen by config.

The engine reads runtime config secrets from env (ESO or compose fills it). This module
is the WRITE seam for secrets the engine generates (the JWT signing key, the ClickHouse
service, org and group passwords, OIDC client secrets): they go to ``scalo.secrets``,
provider selected by ``settings.secrets``. ``file`` is the default, a directory on the
engine's volume. ``ansible_vault`` is an encrypted local file, ``openbao``/``vault`` a
KV mount the deployment configures, and ``aws`` AWS Secrets Manager with every name
under the configured name prefix.

The engine never imports a backend SDK - scalo owns that (see docs/deployment/backing-services.md).
"""

import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from scalo.logger import logger
from scalo.secrets import SecretsManager
from scalo.secrets.exceptions import (
    ProviderError,
    SecretAlreadyExistsError,
    SecretNotFoundError,
    SecretPendingDeletionError,
)

from dfe_engine.settings import SecretsSettings

# Providers whose "path" is a real filesystem path - prefix with the configured root.
_ROOTED = {"file", "ansible_vault"}

# AWS finishes a forced delete in the background, so a write to that name waits up to 30s for it.
FORCED_DELETE_BACKOFF: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 15.0)


class SecretNameHeldError(ProviderError):
    """A deleted secret that has not finished deleting holds a name the engine reads or writes."""

    def __init__(self, provider: str, path: str, window_days: int) -> None:
        """Name the held secret, the remedy and the recovery window setting.

        Args:
            provider: The secrets provider that refused.
            path: The full name of the held secret.
            window_days: The recovery window the engine deletes with.
        """
        super().__init__(
            provider,
            f"secret {path!r} is scheduled for deletion, so its name stays taken until the "
            "deletion finishes. Restore the secret, or wait out its recovery window, then retry. "
            f"The engine deletes with DFE_SECRETS_RECOVERY_WINDOW_DAYS={window_days}, and 0 "
            "deletes with no recovery window",
        )


class DfeSecrets:
    """Provider-agnostic accessor for secrets the engine mints.

    ``put`` upserts (minted secrets rotate), ``create`` refuses to overwrite, ``get``
    returns the UTF-8 string and ``delete`` removes it. File-based providers store under
    the configured root, openbao under its KV mount and aws under the configured name
    prefix. A secret the engine creates on aws carries the configured tags.
    """

    def __init__(
        self,
        manager: SecretsManager,
        provider: str,
        root: str,
        mount: str = "",
        *,
        name_prefix: str = "",
        tags: dict[str, str] | None = None,
        recovery_window_days: int = 0,
        backoff: Sequence[float] = FORCED_DELETE_BACKOFF,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Bind a scalo manager to one provider.

        Args:
            manager: The scalo secrets manager holding the provider.
            provider: The provider every call goes to.
            root: The file and ansible_vault root directory.
            mount: The openbao KV mount.
            name_prefix: The aws name prefix.
            tags: Tags each secret the engine creates on aws carries.
            recovery_window_days: The aws delete window. With 0 a write waits out a forced
                delete of the same name, and with a window it refuses without waiting.
            backoff: The waits, in seconds, between write attempts on a name being deleted.
            sleep: Waits out one backoff step.
        """
        self._mgr = manager
        self._provider = provider
        self._root = root
        self._mount = mount
        self._name_prefix = name_prefix
        # OpenBao stores tags after the value, so a failed tag write would follow a stored secret.
        self._tags = dict(tags) if tags and provider == "aws" else None
        self._window_days = recovery_window_days
        self._backoff = tuple(backoff)
        self._sleep = sleep

    def _full(self, path: str) -> str:
        if self._provider in _ROOTED:
            return str(Path(self._root) / path)
        if self._provider in ("openbao", "vault") and self._mount:
            # openbao KV path = <kv-mount>/<path> (the provider inserts /data/ for KV v2)
            return f"{self._mount}/{path}"
        if self._provider == "aws":
            return f"{self._name_prefix}/{path}"
        return path

    def _write(self, full: str, write: Callable[[], object]) -> None:
        """Run *write*, waiting out a forced delete of *full* that AWS has not finished.

        Raises:
            SecretNameHeldError: The name is still being deleted once the backoff is spent,
                or a recovery window holds it.
        """
        # A windowed delete holds the name for days, so only a forced one is worth waiting out.
        waits = self._backoff if self._window_days == 0 else ()
        for wait in waits:
            try:
                write()
                return
            except SecretPendingDeletionError:
                self._sleep(wait)
        try:
            write()
        except SecretPendingDeletionError as exc:
            raise SecretNameHeldError(self._provider, full, self._window_days) from exc

    def _upsert(self, full: str, data: bytes) -> None:
        try:
            self._mgr.create_sync(full, data, tags=self._tags, provider=self._provider)
        except SecretAlreadyExistsError:
            self._mgr.update_sync(full, data, provider=self._provider)

    def put(self, path: str, value: str) -> None:
        """Create or update a secret (upsert)."""
        full = self._full(path)
        data = value.encode("utf-8")
        self._write(full, lambda: self._upsert(full, data))
        # scalo keeps an in-memory read cache that writes do not invalidate;
        # clear it so a rotated secret is read fresh.
        self._mgr.clear_cache()

    def create(self, path: str, value: str) -> None:
        """Create a secret, leaving one already stored at *path* as it is.

        Raises:
            SecretAlreadyExistsError: A secret is already stored at *path*.
        """
        full = self._full(path)
        data = value.encode("utf-8")
        self._write(
            full,
            lambda: self._mgr.create_sync(full, data, tags=self._tags, provider=self._provider),
        )
        self._mgr.clear_cache()

    def get(self, path: str) -> str:
        """Fetch a secret as a string. Raises SecretNotFoundError if absent."""
        # openbao KV v2 wraps the value under a "value" field; file stores it raw.
        key = "value" if self._provider in ("openbao", "vault") else None
        full = self._full(path)
        try:
            stored = self._mgr.get_sync(full, key=key, provider=self._provider)
        except SecretPendingDeletionError as exc:
            raise SecretNameHeldError(self._provider, full, self._window_days) from exc
        return stored.decode("utf-8")

    def get_or_none(self, path: str) -> str | None:
        """Fetch a secret as a string, or None when nothing is stored at *path*.

        With no recovery window a secret still being deleted reads as gone, so the next
        write waits for its name rather than failing on its value.
        """
        try:
            return self.get(path)
        except SecretNotFoundError:
            return None
        except SecretNameHeldError:
            if self._window_days:
                raise
            return None

    def exists(self, path: str) -> bool:
        """Whether a secret is stored at *path*."""
        return self.get_or_none(path) is not None

    def delete(self, path: str) -> None:
        """Remove a secret (no error if it is already gone or already scheduled for deletion)."""
        with suppress(SecretNotFoundError, SecretPendingDeletionError):
            self._mgr.delete_sync(self._full(path), provider=self._provider)
        self._mgr.clear_cache()


def _to_scalo_config(cfg: SecretsSettings) -> dict[str, Any]:
    """Map DFE SecretsSettings -> scalo.secrets from_config dict (file needs none)."""
    # Every read goes to the store, so a rotation or an outage shows on the next read.
    scfg: dict[str, Any] = {"cache": {"enabled": False}}
    if cfg.provider in ("openbao", "vault"):
        # AppRole in prod (role_id in config; the secret_id is itself a secret and
        # comes from the VAULT_SECRET_ID env, injected by ESO/infra - never config).
        # cfg.mount is the KV secrets mount (prefixes the path), NOT the auth mount.
        auth: dict[str, Any] = {"method": "approle" if cfg.role else "token"}
        if cfg.role:
            auth["role_id"] = cfg.role
        scfg["openbao"] = {"address": cfg.addr, "auth": auth}
    elif cfg.provider == "aws":
        # Credentials come from the SDK chain (pod identity, profile or env), never config.
        aws: dict[str, Any] = {
            "region": cfg.region,
            "recovery_window_days": cfg.recovery_window_days,
        }
        if cfg.kms_key_id:
            aws["kms_key_id"] = cfg.kms_key_id
        scfg["aws"] = aws
    elif cfg.provider == "ansible_vault":
        # the vault password comes from the env
        scfg["ansible_vault"] = {}
    # "file" needs no config block (always available).
    return scfg


def build_secrets(cfg: SecretsSettings) -> DfeSecrets:
    """Build the DFE secrets accessor for the configured backend."""
    manager = SecretsManager.from_config(_to_scalo_config(cfg))
    return DfeSecrets(
        manager,
        provider=cfg.provider,
        root=cfg.path,
        mount=cfg.mount,
        name_prefix=cfg.name_prefix,
        tags=cfg.tags,
        recovery_window_days=cfg.recovery_window_days,
    )


def warn_on_aws_managed_key(cfg: SecretsSettings) -> None:
    """Warn when AWS secrets will be encrypted with the AWS managed key, not the deployment's own."""
    if cfg.provider == "aws" and not cfg.kms_key_id:
        logger.warning(
            "AWS secrets are encrypted with the AWS managed key: set DFE_SECRETS_KMS_KEY_ID "
            "to encrypt them with the deployment's own KMS key",
        )
