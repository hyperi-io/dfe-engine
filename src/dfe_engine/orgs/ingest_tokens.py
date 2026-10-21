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

No token is ever logged, and none is read back out of the API after the request
that mints it.
"""

import secrets as random_tokens
from collections.abc import Iterable
from typing import TYPE_CHECKING

from scalo.logger import logger

from dfe_engine.orgs.ingest_credentials import BEARER_LISTENERS, IngestCredential
from dfe_engine.secrets import DfeSecrets

if TYPE_CHECKING:
    from dfe_engine.orgs.registry import OrgRegistry

CREDENTIAL_SECRET_PREFIX = "ingest/credentials"
BEARER_SECRET_PREFIX = "ingest/bearer-tokens"

# 32 random bytes, URL-safe base64: no ':' and no line break, the receiver's two separators.
_TOKEN_BYTES = 32


def credential_secret_path(name: str) -> str:
    """Where the token of the credential *name* is stored."""
    return f"{CREDENTIAL_SECRET_PREFIX}/{name}"


def bearer_secret_path(listener: str) -> str:
    """Where the receiver reads *listener*'s bearer token entries."""
    return f"{BEARER_SECRET_PREFIX}/{listener}"


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


def accepted_credentials(registry: OrgRegistry) -> list[IngestCredential]:
    """The credentials the receiver accepts: those whose owning org is registered and enabled."""
    enabled = {org.name for org in registry.list() if org.enabled}
    return [c for c in registry.ingest_credentials.list() if c.org in enabled]


def sync_bearer_tokens(registry: OrgRegistry, secrets: DfeSecrets) -> list[str]:
    """Rewrite each listener's bearer secret that no longer matches the accepted credentials.

    Args:
        registry: The org registry holding the credentials.
        secrets: The secrets seam.

    Returns:
        The listeners whose secret was rewritten.
    """
    rewritten = []
    for listener, content in bearer_entries(accepted_credentials(registry), secrets).items():
        path = bearer_secret_path(listener)
        if secrets.get_or_none(path) == content:
            continue
        secrets.put(path, content)
        rewritten.append(listener)
    return rewritten
