#  Project:      dfe-engine
#  File:         synthetic_data/credential.py
#  Purpose:      The ingest credential the engine's synthetic-data streams present to dfe-receiver
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The ingest credential the engine's synthetic-data streams present to dfe-receiver.

The credential is owned by and binds the primary org, on the HTTP listener, so a stream's
records land in it wherever that listener authenticates. It never turns the listener's auth
on (:func:`~dfe_engine.orgs.ingest_tokens.compile_listener_auth`), so a stream never makes
another sender present a token.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from scalo.logger import logger
from scalo.secrets import SecretsError

from dfe_engine.orgs.ingest_credentials import (
    SYNTHETIC_DATA_CREDENTIAL,
    CredentialChangedError,
    CredentialIdTakenError,
    IngestCredential,
)
from dfe_engine.orgs.ingest_tokens import (
    BEARER_REFRESH_SECONDS,
    credential_secret_path,
    drop_token,
    mint_token,
    new_token_id,
    store_token,
)

if TYPE_CHECKING:
    from dfe_engine.orgs.registry import OrgRegistry
    from dfe_engine.secrets import DfeSecrets

# The receiver re-reads its tokens every BEARER_REFRESH_SECONDS, so it may refuse a new one until then.
REFUSED_GRACE_SECONDS = BEARER_REFRESH_SECONDS + 30


@dataclass(frozen=True, slots=True)
class StreamCredential:
    """The token a synthetic-data stream presents, whether this call minted it and the epoch second until which the receiver may still refuse it."""

    minted: bool
    refused_until: float
    token: str


def _held(*, org: str, registry: OrgRegistry, secrets: DfeSecrets) -> StreamCredential | None:
    """The stored credential, where it is the one :func:`_wanted` describes and its token is stored."""
    stored = registry.ingest_credentials.get(SYNTHETIC_DATA_CREDENTIAL)
    if (stored is None) or (stored.content() != _wanted(org=org).content()):
        return None
    token = secrets.get_or_none(credential_secret_path(credential=stored))
    if token is None:
        return None
    return StreamCredential(
        minted=False, refused_until=_refused_until(rotated_at=stored.rotated_at), token=token
    )


def _held_or_minted(
    *, org: str, registry: OrgRegistry, secrets: DfeSecrets
) -> StreamCredential | None:
    """The held credential, else a new one, else the one another stream minted or removed first."""
    held = _held(org=org, registry=registry, secrets=secrets)
    if held is not None:
        return held
    try:
        return _mint(org=org, registry=registry, secrets=secrets)
    except CredentialChangedError, CredentialIdTakenError, KeyError:
        return _held(org=org, registry=registry, secrets=secrets)


def _mint(*, org: str, registry: OrgRegistry, secrets: DfeSecrets) -> StreamCredential:
    """Mint the credential for *org* in place of any stored one, storing the token before the record that names it."""
    stored = registry.ingest_credentials.get(SYNTHETIC_DATA_CREDENTIAL)
    if (stored is not None) and (stored.org != org):
        # A put refuses to change a credential's owner, so the old one goes first.
        registry.ingest_credentials.delete(SYNTHETIC_DATA_CREDENTIAL, current=stored)
        drop_token(credential=stored, secrets=secrets)
        stored = None
    now = datetime.now(tz=UTC).isoformat()
    credential = _wanted(org=org).model_copy(
        update={
            "created_at": now if stored is None else stored.created_at,
            "rotated_at": now,
            "token_id": new_token_id(),
        }
    )
    token = mint_token()
    store_token(credential=credential, secrets=secrets, token=token)
    try:
        registry.ingest_credentials.put(credential=credential, current=stored, new=stored is None)
    except BaseException:
        drop_token(credential=credential, secrets=secrets)
        raise
    if stored is not None:
        drop_token(credential=stored, secrets=secrets)
    return StreamCredential(minted=True, refused_until=_refused_until(rotated_at=now), token=token)


def _refused_until(*, rotated_at: str) -> float:
    """The epoch second by which the receiver holds a token minted at *rotated_at*, 0 for a time that does not parse."""
    try:
        return datetime.fromisoformat(rotated_at).timestamp() + REFUSED_GRACE_SECONDS
    except ValueError:
        return 0.0


def _wanted(*, org: str) -> IngestCredential:
    """The credential as streams need it: owned by and binding *org*, on the HTTP listener."""
    return IngestCredential(
        default_org=org, listeners=["http"], name=SYNTHETIC_DATA_CREDENTIAL, org=org, orgs=[org]
    )


def stream_credential(*, registry: OrgRegistry, secrets: DfeSecrets) -> StreamCredential | None:
    """The primary org's synthetic-data credential, minted where it is missing, tokenless or binds anything else. None with no primary org named, or no backend to hold it."""
    try:
        org = registry.home_name()
    except ValueError as exc:
        logger.warning(
            "synthetic data streams without a credential: the primary org cannot be read",
            error=str(exc),
        )
        return None
    if org is None:
        return None
    try:
        return _held_or_minted(org=org, registry=registry, secrets=secrets)
    except SecretsError as exc:
        logger.warning(
            "synthetic data streams without a credential: the secrets backend refused",
            error=str(exc),
        )
        return None
