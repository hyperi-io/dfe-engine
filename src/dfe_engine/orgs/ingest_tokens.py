#  Project:      dfe-engine
#  File:         orgs/ingest_tokens.py
#  Purpose:      Mint ingest credential tokens and keep the receiver's bearer secrets in step
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Ingest credential tokens, minted through the engine's secrets seam.

Each credential's token is its own secret at ``ingest/credentials/<id>``. The
receiver reads one secret per listener, ``ingest/bearer-tokens/<listener>``, holding
an ``<id>:<token>`` line for every credential that listener accepts, so the id in
front of the token names the binding the receiver holds the record to. The
per-listener secrets are derived: :func:`sync_bearer_tokens` rewrites them from the
credentials after every change, so a deleted credential's token, or a disabled org's,
stops working whichever writer made the change.

The receiver authenticates a listener only while an accepted credential lists it.
:func:`compile_listener_auth` compiles ``mode: bearer`` on that listener's secret then,
and ``mode: none`` otherwise, under which a record lands in the default org.

On Kubernetes the ``file`` backend writes under the engine's own volume, which no
receiver pod can mount. With ``DFE_INGEST_BEARER_SECRET`` set, :func:`sync_bearer_tokens`
also writes each listener's content into that Kubernetes Secret, keyed by listener, and
the receiver mounts it at :data:`RECEIVER_BEARER_MOUNT`. Every startup and every apps
reconcile syncs again, so a Secret recreated empty is refilled without a credential write.

No token is ever logged, and none is read back out of the API after the request
that mints it.
"""

import secrets as random_tokens
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from scalo.logger import logger
from scalo.secrets import (
    SecretAlreadyExistsError,
    SecretNotFoundError,
    SecretsError,
    SecretsManager,
)

from dfe_engine.orgs.ingest_credentials import BEARER_LISTENERS, IngestCredential, IngestListener
from dfe_engine.orgs.tenancy import Tenancy
from dfe_engine.secrets import DfeSecrets

if TYPE_CHECKING:
    from dfe_engine.orgs.registry import OrgRegistry
    from dfe_engine.settings import SecretsSettings

CREDENTIAL_SECRET_PREFIX = "ingest/credentials"
BEARER_SECRET_PREFIX = "ingest/bearer-tokens"

RECEIVER_BEARER_MOUNT = "/var/run/secrets/dfe/ingest-bearer-tokens"
"""Where dfe-receiver mounts the ``file`` backend's bearer tokens, read-only, one file per listener.

The ``file`` backend writes under the engine's own volume, so the receiver reads the
directory where the deployment mounts it rather than where the engine wrote it: the
backend's ``ingest/bearer-tokens`` directory on docker, the mirror Secret on Kubernetes.
"""

MIRROR_PROVIDER = "kubernetes"
"""The scalo secrets provider that writes the mirror Secret's keys."""

BEARER_REFRESH_SECONDS = 60
"""How often dfe-receiver re-reads a listener's bearer secret: a deleted, disabled or
rotated credential's token stops within this, plus a mounted Secret's kubelet sync."""

# 32 random bytes, URL-safe base64: no ':' and no line break, the receiver's two separators.
_TOKEN_BYTES = 32


class BearerSourceUnreadableError(ValueError):
    """The engine's secrets backend holds the bearer tokens where dfe-receiver cannot read them."""


def credential_secret_path(name: str) -> str:
    """Where the token of the credential *name* is stored."""
    return f"{CREDENTIAL_SECRET_PREFIX}/{name}"


def bearer_secret_path(listener: str) -> str:
    """Where the receiver reads *listener*'s bearer token entries."""
    return f"{BEARER_SECRET_PREFIX}/{listener}"


def bearer_secret_source(listener: str, backend: SecretsSettings) -> str:
    """dfe-receiver's ``secret_source`` for *listener*'s bearer secret, where *backend* holds it.

    The spec names the secret as :class:`~dfe_engine.secrets.DfeSecrets` writes it: under
    the KV mount on OpenBao, under the name prefix on AWS Secrets Manager. A ``file``
    secret is named under :data:`RECEIVER_BEARER_MOUNT`. An ``aws:`` spec resolves only
    in a receiver built with scalo's ``secrets-aws``.

    Raises:
        BearerSourceUnreadableError: *backend* is one of scalo-py's providers that
            dfe-receiver reads no secret from, or no provider scalo-py has.
    """
    path = bearer_secret_path(listener)
    match backend.provider:
        case "openbao" | "vault":
            stored = f"{backend.mount}/{path}" if backend.mount else path
            return f"vault:{stored}:value"
        case "aws":
            return f"aws:{backend.name_prefix}/{path}"
        case "file":
            return f"file:{RECEIVER_BEARER_MOUNT}/{listener}"
        case "gcp" | "azure":
            reason = "it has no provider for that backend"
        case "ansible_vault":
            reason = "it cannot decrypt an ansible-vault file"
        case _:
            reason = "that is not a scalo secrets provider"
    raise BearerSourceUnreadableError(
        f"dfe-receiver cannot read the ingest bearer tokens on "
        f"DFE_SECRETS_PROVIDER={backend.provider}: {reason}. Use openbao or aws, or file with "
        f"the receiver mounting DFE_SECRETS_PATH/{BEARER_SECRET_PREFIX}, or on Kubernetes the "
        f"DFE_INGEST_BEARER_SECRET Secret, at {RECEIVER_BEARER_MOUNT}"
    )


def compile_listener_auth(
    tenancy: Tenancy, backend: SecretsSettings
) -> dict[IngestListener, dict[str, Any]]:
    """Each bearer listener's dfe-receiver ``auth`` block.

    ``mode: bearer`` on the listener's secret while an accepted credential lists the
    listener, else ``mode: none``, under which the receiver lands a record in the
    default org. :func:`sync_bearer_tokens` writes the secret from the same accepted
    credentials, so the mode and the secret agree on which ones a listener takes. The
    receiver re-reads it every :data:`BEARER_REFRESH_SECONDS`.

    Args:
        tenancy: The orgs and credentials to compile.
        backend: The engine's secrets backend, which holds each listener's secret.

    Returns:
        The block by listener, every listener present.

    Raises:
        BearerSourceUnreadableError: A listener needs its secret and *backend* holds it
            where dfe-receiver cannot read it.
    """
    listed: set[str] = set()
    for credential in accepted_credentials(tenancy):
        listed.update(credential.listeners)
    blocks: dict[IngestListener, dict[str, Any]] = {}
    for listener in BEARER_LISTENERS:
        if listener not in listed:
            blocks[listener] = {"mode": "none"}
            continue
        bearer = {
            "secret_source": bearer_secret_source(listener, backend),
            "refresh_interval_secs": BEARER_REFRESH_SECONDS,
        }
        blocks[listener] = {"mode": "bearer", "bearer": bearer}
    return blocks


def mint_token() -> str:
    """A new bearer token."""
    return random_tokens.token_urlsafe(_TOKEN_BYTES)


def store_token(secrets: DfeSecrets, name: str, token: str) -> None:
    """Store *token* as the credential *name*'s, replacing any it had."""
    secrets.put(credential_secret_path(name), token)


def drop_token(secrets: DfeSecrets, name: str) -> None:
    """Remove the credential *name*'s token, where one is stored."""
    secrets.delete(credential_secret_path(name))


def bearer_entries(credentials: Iterable[IngestCredential], secrets: DfeSecrets) -> dict[str, str]:
    """Each listener's bearer secret content: one ``<id>:<token>`` line per credential.

    A credential whose token is not stored is left out and logged: it has nothing a
    sender could present.

    Args:
        credentials: Every ingest credential.
        secrets: The secrets seam holding the tokens.

    Returns:
        Content by listener, every listener present, lines sorted by id and ending
        in a newline so the secret is never empty.
    """
    lines: dict[str, list[str]] = {listener: [] for listener in BEARER_LISTENERS}
    for credential in sorted(credentials, key=lambda c: c.name):
        token = secrets.get_or_none(credential_secret_path(credential.name))
        if token is None:
            logger.warning("ingest credential has no stored token", ingest_id=credential.name)
            continue
        for listener in credential.listeners:
            lines[listener].append(f"{credential.name}:{token}")
    return {
        listener: "".join(f"{line}\n" for line in found) or "\n"
        for listener, found in lines.items()
    }


def accepted_credentials(tenancy: Tenancy) -> list[IngestCredential]:
    """The credentials the receiver accepts: those whose owning org is registered and enabled."""
    enabled = {org.name for org in tenancy.orgs if org.enabled}
    return [c for c in tenancy.credentials if c.org in enabled]


def build_bearer_mirror(secret_name: str) -> SecretsManager | None:
    """The scalo secrets manager writing the mirror Secret *secret_name*, None when it is unset.

    It authenticates as the engine's own service account, in the engine's own namespace.
    Every read goes to the API server, so a Secret reset under it is seen on the next sync.
    """
    if not secret_name:
        return None
    return SecretsManager.from_config(
        {MIRROR_PROVIDER: {"secret_name": secret_name}, "cache": {"enabled": False}}
    )


def mirror_bearer_tokens(entries: dict[str, str], mirror: SecretsManager) -> list[str]:
    """Write each listener's content into the mirror Secret where the Secret holds something else.

    Compared with the Secret itself rather than the engine's backend, so a Secret reset
    or a write that failed earlier is caught up. A key is only ever written, never removed:
    the receiver keeps its last tokens when a file it reads disappears.

    Args:
        entries: Content by listener, as :func:`bearer_entries` builds it.
        mirror: The manager :func:`build_bearer_mirror` returned.

    Returns:
        The listeners whose key was written.

    Raises:
        SecretsError: The Secret does not exist, the engine may not read or update it,
            or the API server could not be reached.
    """
    written = []
    for listener, content in entries.items():
        value = content.encode("utf-8")
        try:
            held = mirror.get_sync(listener, provider=MIRROR_PROVIDER).data
        except SecretNotFoundError:
            held = None
        if held == value:
            continue
        if held is None:
            try:
                mirror.create_sync(listener, value, provider=MIRROR_PROVIDER)
            except SecretAlreadyExistsError:
                mirror.update_sync(listener, value, provider=MIRROR_PROVIDER)
        else:
            mirror.update_sync(listener, value, provider=MIRROR_PROVIDER)
        written.append(listener)
    if written:
        logger.info("receiver bearer tokens written to the mirror Secret", listeners=written)
    return written


def sync_bearer_tokens(
    registry: OrgRegistry, secrets: DfeSecrets, mirror: SecretsManager | None = None
) -> list[str]:
    """Rewrite each listener's bearer secret that no longer matches the accepted credentials.

    Args:
        registry: The org registry holding the credentials.
        secrets: The secrets seam.
        mirror: The mirror Secret's manager, None when no mirror is configured.

    Returns:
        The listeners whose secret, or mirror Secret key, was rewritten.

    Raises:
        SecretsError: The backend or the mirror Secret refused. The backend is written
            first, so a refused mirror leaves it current and the next sync retries the mirror.
    """
    rewritten: set[str] = set()
    accepted = accepted_credentials(Tenancy.read(registry))
    entries = bearer_entries(accepted, secrets)
    for listener, content in entries.items():
        path = bearer_secret_path(listener)
        if secrets.get_or_none(path) == content:
            continue
        secrets.put(path, content)
        rewritten.add(listener)
    if mirror is not None:
        rewritten.update(mirror_bearer_tokens(entries, mirror))
    return [listener for listener in BEARER_LISTENERS if listener in rewritten]


def resync_bearer_tokens(
    registry: OrgRegistry, secrets: DfeSecrets, mirror: SecretsManager | None
) -> None:
    """Bring the bearer secrets back into step with no credential write behind it. Never raises.

    A refusal is logged and left to the next startup, reconcile or credential write.
    """
    try:
        rewritten = sync_bearer_tokens(registry, secrets, mirror)
    except SecretsError as exc:
        logger.warning("receiver bearer tokens not brought back into step", error=str(exc))
        return
    if rewritten:
        logger.info("receiver bearer tokens brought back into step", listeners=rewritten)
