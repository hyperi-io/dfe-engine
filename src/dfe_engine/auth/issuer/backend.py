#  Project:      dfe-engine
#  File:         auth/issuer/backend.py
#  Purpose:      Issuer-agnostic identity-management interface (users/connectors/sessions)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Issuer-agnostic identity-management interface.

The engine is the sole control plane for the bundled OIDC issuer. Everything
above this module -- the HTTP API, the ``dfe`` CLI -- speaks these engine terms
(users, connectors, sessions) and never the issuer's own vocabulary, so the
issuer can be swapped without an API change. ``dex`` is the one implementation
today (``dfe_engine.auth.issuer.dex``).

Semantic outcomes (a user already exists, a connector is not found) are returned
as typed results, not raised -- they are normal control-plane states and make
seeding idempotent. Only transport/unexpected failures raise ``IssuerError``.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


class IssuerError(RuntimeError):
    """A management call to the issuer failed at the transport layer.

    Semantic outcomes (already-exists, not-found) do NOT raise -- they come back
    in the typed result. This is reserved for an unreachable issuer, a TLS/mTLS
    failure, or an unexpected status.
    """


@dataclass(frozen=True)
class IssuerUser:
    """A local user held by the issuer (the credential lives there, not here).

    ``subject`` is the stable OIDC ``sub`` the issuer mints for this user; role
    bindings in the engine key off it (issuer local users carry no groups).
    """

    email: str
    username: str = ""
    subject: str = ""


@dataclass(frozen=True)
class IssuerConnector:
    """A federation connector (an upstream IdP the issuer delegates login to)."""

    id: str
    type: str
    name: str
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IssuerSession:
    """A live refresh-token reference for a user, for listing and revocation."""

    id: str
    client_id: str
    created_at: int = 0
    last_used: int = 0


class IssuerBackend(abc.ABC):
    """Manage the issuer's users, connectors and sessions in engine terms.

    All methods are async. Idempotent by design: ``create_*`` returns False when
    the entry already existed, ``delete_*``/``update_*`` return False when the
    target was absent -- callers seed and reconcile without catching.
    """

    # -- Users (local, credential-holding) --------------------------------

    @abc.abstractmethod
    async def create_user(
        self, *, email: str, password: str, username: str = "", subject: str = ""
    ) -> bool:
        """Create a local user from a PLAINTEXT password (hashed by the backend).

        Returns True when created, False when a user with that email already
        existed. Raises IssuerError on transport failure.
        """

    @abc.abstractmethod
    async def update_user(
        self, *, email: str, new_password: str | None = None, new_username: str | None = None
    ) -> bool:
        """Update a user's password and/or username. Returns False if not found."""

    @abc.abstractmethod
    async def delete_user(self, *, email: str) -> bool:
        """Delete a user by email. Returns False if not found."""

    @abc.abstractmethod
    async def list_users(self) -> list[IssuerUser]:
        """List all local users."""

    @abc.abstractmethod
    async def verify_user(self, *, email: str, password: str) -> bool:
        """Return True iff the plaintext password matches the stored hash."""

    # -- Connectors (federation) ------------------------------------------

    @abc.abstractmethod
    async def create_connector(
        self, *, id: str, type: str, name: str, config: dict[str, Any]
    ) -> bool:
        """Create a federation connector. Returns False if the id already existed."""

    @abc.abstractmethod
    async def update_connector(
        self,
        *,
        id: str,
        new_type: str | None = None,
        new_name: str | None = None,
        new_config: dict[str, Any] | None = None,
    ) -> bool:
        """Update a connector. Returns False if not found."""

    @abc.abstractmethod
    async def delete_connector(self, *, id: str) -> bool:
        """Delete a connector by id. Returns False if not found."""

    @abc.abstractmethod
    async def list_connectors(self) -> list[IssuerConnector]:
        """List all federation connectors."""

    # -- Sessions (refresh tokens) ----------------------------------------

    @abc.abstractmethod
    async def list_sessions(self, *, subject: str) -> list[IssuerSession]:
        """List a user's live refresh-token sessions, keyed by OIDC subject."""

    @abc.abstractmethod
    async def revoke_session(self, *, subject: str, client_id: str) -> bool:
        """Revoke the refresh token for a subject/client pair. False if not found."""

    # -- Lifecycle --------------------------------------------------------

    @abc.abstractmethod
    async def health(self) -> tuple[str, int]:
        """Return (server_version, api_version); a live round trip to the issuer."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Release the underlying transport."""
