#  Project:      dfe-engine
#  File:         auth/issuer/dex/client.py
#  Purpose:      Dex implementation of the issuer backend, over mutual-TLS gRPC
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Dex implementation of :class:`IssuerBackend`, over mutual-TLS gRPC.

Maps the engine's issuer-agnostic vocabulary onto dex's ``Dex`` gRPC service.
Local users are dex ``Password`` entries (email + bcrypt hash), connectors are
dex ``Connector`` entries (JSON config), sessions are dex refresh tokens.

The channel is mutual-TLS: the engine presents a client cert issued by the SAME
cluster CA that signed dex's server cert, so trust is symmetric. Certs are read
from files (mounted secrets); the channel is built lazily on first use and
reused. Transport failures surface as :class:`IssuerError`; already-exists /
not-found are normal outcomes and come back in the return value.
"""

from __future__ import annotations

import json
from pathlib import Path

import bcrypt
import grpc
from grpc import aio
from scalo.logger import logger

from dfe_engine.auth.issuer.backend import (
    IssuerBackend,
    IssuerConnector,
    IssuerError,
    IssuerSession,
    IssuerUser,
)
from dfe_engine.auth.issuer.dex.proto import api_pb2 as pb
from dfe_engine.auth.issuer.dex.proto import api_pb2_grpc as pb_grpc


def _hash_password(password: str) -> bytes:
    """Return a bcrypt hash of the plaintext, the shape dex stores."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())


class DexIssuerBackend(IssuerBackend):
    """Manage dex over mutual-TLS gRPC in the engine's issuer-agnostic terms."""

    def __init__(
        self,
        *,
        endpoint: str,
        ca_cert: str,
        client_cert: str,
        client_key: str,
        server_name: str = "",
    ) -> None:
        """Configure the mTLS channel to a dex gRPC endpoint.

        Args:
            endpoint: host:port of the dex gRPC service (e.g. dfe-dex.dex:5557).
            ca_cert: PEM file of the CA that signed dex's server cert.
            client_cert: PEM file of the engine's client cert (same CA).
            client_key: PEM file of the engine's client private key.
            server_name: override the TLS authority when the endpoint host is
                not itself a SAN on dex's server cert (empty = use the endpoint).
        """
        self._endpoint = endpoint
        self._ca_cert = ca_cert
        self._client_cert = client_cert
        self._client_key = client_key
        self._server_name = server_name
        self._channel: aio.Channel | None = None
        self._stub: pb_grpc.DexStub | None = None

    # -- channel lifecycle ------------------------------------------------

    def _ensure_stub(self) -> pb_grpc.DexStub:
        """Build the mTLS channel + stub once, then reuse it."""
        if self._stub is not None:
            return self._stub
        try:
            creds = grpc.ssl_channel_credentials(
                root_certificates=Path(self._ca_cert).read_bytes(),
                private_key=Path(self._client_key).read_bytes(),
                certificate_chain=Path(self._client_cert).read_bytes(),
            )
        except OSError as exc:
            raise IssuerError(f"issuer mTLS material unreadable: {exc}") from exc
        options = (
            [("grpc.ssl_target_name_override", self._server_name)] if self._server_name else []
        )
        self._channel = aio.secure_channel(self._endpoint, creds, options=options)
        self._stub = pb_grpc.DexStub(self._channel)
        logger.debug("issuer gRPC channel opened", endpoint=self._endpoint)
        return self._stub

    async def close(self) -> None:
        """Close the channel if it was opened."""
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None

    # -- users ------------------------------------------------------------

    async def create_user(
        self, *, email: str, password: str, username: str = "", subject: str = ""
    ) -> bool:
        """Create a dex Password entry from the plaintext (bcrypt-hashed here)."""
        req = pb.CreatePasswordReq(
            password=pb.Password(
                email=email,
                hash=_hash_password(password),
                username=username or email,
                user_id=subject,
            )
        )
        resp = await self._call("CreatePassword", self._ensure_stub().CreatePassword, req)
        return not resp.already_exists

    async def update_user(
        self, *, email: str, new_password: str | None = None, new_username: str | None = None
    ) -> bool:
        """Update the user's password and/or username, preserving the unset field."""
        if new_password is None and new_username is None:
            return True
        # dex UpdatePassword overwrites BOTH hash and username, so preserve the
        # field the caller did not supply by reading the current entry first.
        current = None
        if new_password is None or new_username is None:
            current = await self._find_user(email)
            if current is None:
                return False
        new_hash = _hash_password(new_password) if new_password is not None else b""
        req = pb.UpdatePasswordReq(
            email=email,
            new_hash=new_hash if new_password is not None else current.hash,
            new_username=new_username if new_username is not None else current.username,
        )
        resp = await self._call("UpdatePassword", self._ensure_stub().UpdatePassword, req)
        return not resp.not_found

    async def delete_user(self, *, email: str) -> bool:
        """Delete the dex Password for this email; False when it was absent."""
        req = pb.DeletePasswordReq(email=email)
        resp = await self._call("DeletePassword", self._ensure_stub().DeletePassword, req)
        return not resp.not_found

    async def list_users(self) -> list[IssuerUser]:
        """List all dex Password entries as issuer users."""
        resp = await self._call(
            "ListPasswords", self._ensure_stub().ListPasswords, pb.ListPasswordReq()
        )
        return [
            IssuerUser(email=p.email, username=p.username, subject=p.user_id)
            for p in resp.passwords
        ]

    async def verify_user(self, *, email: str, password: str) -> bool:
        """Check the plaintext against the stored bcrypt hash via dex."""
        req = pb.VerifyPasswordReq(email=email, password=password)
        resp = await self._call("VerifyPassword", self._ensure_stub().VerifyPassword, req)
        return resp.verified and not resp.not_found

    async def _find_user(self, email: str) -> pb.Password | None:
        """Return the raw dex Password for an email (carries the hash), or None."""
        resp = await self._call(
            "ListPasswords", self._ensure_stub().ListPasswords, pb.ListPasswordReq()
        )
        for p in resp.passwords:
            if p.email == email:
                return p
        return None

    # -- connectors -------------------------------------------------------

    async def create_connector(
        self, *, id: str, type: str, name: str, config: dict[str, object]
    ) -> bool:
        """Create a dex Connector with the config JSON-serialised to bytes."""
        req = pb.CreateConnectorReq(
            connector=pb.Connector(id=id, type=type, name=name, config=_dump_config(config))
        )
        resp = await self._call("CreateConnector", self._ensure_stub().CreateConnector, req)
        return not resp.already_exists

    async def update_connector(
        self,
        *,
        id: str,
        new_type: str | None = None,
        new_name: str | None = None,
        new_config: dict[str, object] | None = None,
    ) -> bool:
        """Update a connector, preserving the fields the caller left unset."""
        if new_type is None and new_name is None and new_config is None:
            return True
        # dex UpdateConnector overwrites type/name/config together; preserve the
        # unspecified fields from the current entry.
        current = None
        if new_type is None or new_name is None or new_config is None:
            current = await self._find_connector(id)
            if current is None:
                return False
        req = pb.UpdateConnectorReq(
            id=id,
            new_type=new_type if new_type is not None else current.type,
            new_name=new_name if new_name is not None else current.name,
            new_config=_dump_config(new_config) if new_config is not None else current.config,
        )
        resp = await self._call("UpdateConnector", self._ensure_stub().UpdateConnector, req)
        return not resp.not_found

    async def delete_connector(self, *, id: str) -> bool:
        """Delete the dex Connector by id; False when it was absent."""
        req = pb.DeleteConnectorReq(id=id)
        resp = await self._call("DeleteConnector", self._ensure_stub().DeleteConnector, req)
        return not resp.not_found

    async def list_connectors(self) -> list[IssuerConnector]:
        """List all dex connectors, parsing each JSON config back to a dict."""
        resp = await self._call(
            "ListConnectors", self._ensure_stub().ListConnectors, pb.ListConnectorReq()
        )
        return [
            IssuerConnector(id=c.id, type=c.type, name=c.name, config=_load_config(c.config))
            for c in resp.connectors
        ]

    async def _find_connector(self, id: str) -> pb.Connector | None:
        resp = await self._call(
            "ListConnectors", self._ensure_stub().ListConnectors, pb.ListConnectorReq()
        )
        for c in resp.connectors:
            if c.id == id:
                return c
        return None

    # -- sessions ---------------------------------------------------------

    async def list_sessions(self, *, subject: str) -> list[IssuerSession]:
        """List the user's dex refresh tokens, keyed by OIDC subject."""
        req = pb.ListRefreshReq(user_id=subject)
        resp = await self._call("ListRefresh", self._ensure_stub().ListRefresh, req)
        return [
            IssuerSession(
                id=r.id, client_id=r.client_id, created_at=r.created_at, last_used=r.last_used
            )
            for r in resp.refresh_tokens
        ]

    async def revoke_session(self, *, subject: str, client_id: str) -> bool:
        """Revoke the refresh token for a subject/client pair; False if absent."""
        req = pb.RevokeRefreshReq(user_id=subject, client_id=client_id)
        resp = await self._call("RevokeRefresh", self._ensure_stub().RevokeRefresh, req)
        return not resp.not_found

    # -- lifecycle --------------------------------------------------------

    async def health(self) -> tuple[str, int]:
        """Return dex's (server_version, api_version) via a live GetVersion."""
        resp = await self._call("GetVersion", self._ensure_stub().GetVersion, pb.VersionReq())
        return resp.server, resp.api

    # -- internals --------------------------------------------------------

    async def _call(self, op, method, request):
        """One unary RPC; map a transport error to IssuerError, log at debug."""
        try:
            result = await method(request)
        except aio.AioRpcError as exc:
            logger.error("issuer call failed", op=op, code=str(exc.code()), detail=exc.details())
            raise IssuerError(f"issuer {op} failed: {exc.code()}: {exc.details()}") from exc
        logger.debug("issuer call ok", op=op)
        return result


def _dump_config(config: dict[str, object] | None) -> bytes:
    """Serialise a connector config dict to the JSON bytes dex stores."""
    return json.dumps(config or {}).encode("utf-8")


def _load_config(raw: bytes) -> dict[str, object]:
    """Parse a connector's JSON config bytes; empty/invalid -> empty dict."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
