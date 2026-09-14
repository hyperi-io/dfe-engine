#  Project:      dfe-engine
#  File:         auth/jwt_authority.py
#  Purpose:      The DFE engine as the single ES384 JWT issuer + JWKS publisher
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The DFE engine's JWT authority - the one issuer of the cross-app identity token.

Signs the DFE identity token with ECDSA P-384 + SHA-384 (ES384, CNSA-aligned) and
publishes the public key(s) as a JWKS, so every peer - Envoy ``jwt_authn``, dfe-ui,
dfe-hyperdx - verifies by signature, not by trusting a header. Crypto-agile: the
algorithm is config-driven so ML-DSA can replace ES384 once the JOSE + Envoy chain
supports it (see docs/AUTH-ENVOY-TOPOLOGY.md).

The signing private key lives in ``scalo.secrets`` (file provider for dfe-docker,
OpenBao on k8s) via the engine's ``DfeSecrets`` seam - PyJWT does the crypto, scalo
owns the key at rest. The ``kid`` is the RFC 7638 JWK thumbprint: stable, deterministic,
and identical wherever the same public key is seen.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ec import (
    EllipticCurvePrivateKey,
    EllipticCurvePublicKey,
)
from jwt.exceptions import InvalidTokenError
from scalo.logger import logger
from scalo.secrets.exceptions import SecretNotFoundError

from dfe_engine.auth.hyperdx_role import CLAIM as ROLE_CLAIM
from dfe_engine.auth.hyperdx_role import SERVICE_ROLE
from dfe_engine.secrets import DfeSecrets

# ES384 = ECDSA P-384 + SHA-384. The feasible CNSA-2.0 signature for JWTs today
# (Envoy jwt_authn, PyJWT and node jose all verify it; ML-DSA-87 is the roadmap
# target but not yet in the JOSE/Envoy chain). Ed25519 is deliberately NOT offered:
# at 128-bit security it is below the CNSA target.
DEFAULT_ALGORITHM = "ES384"

_CURVE_FOR_ALG: dict[str, type[ec.EllipticCurve]] = {
    "ES256": ec.SECP256R1,
    "ES384": ec.SECP384R1,
    "ES512": ec.SECP521R1,
}
_CRV_NAME = {"ES256": "P-256", "ES384": "P-384", "ES512": "P-521"}
_COORD_BYTES = {"ES256": 32, "ES384": 48, "ES512": 66}
_CLOCK_SKEW_SECONDS = 30

# Machine-token identity for engine -> dfe-hyperdx control calls (dfe-engine#149).
MACHINE_SUBJECT = "svc:dfe-engine"
HYPERDX_AUDIENCE = "dfe-hyperdx"
MACHINE_TOKEN_TTL_SECONDS = 300
_MACHINE_REFRESH_MARGIN_SECONDS = 30


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _public_jwk(public_key: EllipticCurvePublicKey, alg: str) -> dict[str, str]:
    """RFC 7517 JWK for an EC public key, with the RFC 7638 thumbprint as ``kid``."""
    nums = public_key.public_numbers()
    size = _COORD_BYTES[alg]
    crv = _CRV_NAME[alg]
    x = _b64url(nums.x.to_bytes(size, "big"))
    y = _b64url(nums.y.to_bytes(size, "big"))
    # RFC 7638: thumbprint = SHA-256 over the required members, lexicographically
    # sorted, no whitespace. The JWK's four required EC members are crv/kty/x/y.
    canonical = json.dumps(
        {"crv": crv, "kty": "EC", "x": x, "y": y},
        separators=(",", ":"),
        sort_keys=True,
    )
    kid = _b64url(hashlib.sha256(canonical.encode("ascii")).digest())
    return {"kty": "EC", "crv": crv, "x": x, "y": y, "use": "sig", "alg": alg, "kid": kid}


class JwtAuthority:
    """Issues + verifies the DFE identity token and publishes the JWKS.

    One active signing key (loaded from, or minted into, ``scalo.secrets``) plus any
    additional verify-only keys for a rotation overlap. ``verify`` accepts any trusted
    ``kid``; ``sign`` always uses the active key.
    """

    def __init__(
        self,
        secrets: DfeSecrets,
        *,
        issuer: str,
        algorithm: str = DEFAULT_ALGORITHM,
        key_path: str = "jwt/signing-key",
        expire_minutes: int = 60,
    ) -> None:
        if algorithm not in _CURVE_FOR_ALG:
            raise ValueError(
                f"Unsupported JWT algorithm {algorithm!r}; expected one of "
                f"{sorted(_CURVE_FOR_ALG)} (asymmetric only - HS* cannot back a JWKS)"
            )
        self._secrets = secrets
        self._issuer = issuer
        self._alg = algorithm
        self._key_path = key_path
        self._expire_minutes = expire_minutes
        self._private_key = self._load_or_mint()
        self._active_kid = _public_jwk(self._private_key.public_key(), self._alg)["kid"]
        # kid -> public key, for verification. Seeded with the active key; extend via
        # add_verify_key_pem for a rotation overlap.
        self._verify_keys: dict[str, EllipticCurvePublicKey] = {
            self._active_kid: self._private_key.public_key()
        }

    @property
    def kid(self) -> str:
        return self._active_kid

    @property
    def issuer(self) -> str:
        return self._issuer

    @property
    def algorithm(self) -> str:
        return self._alg

    def _load_or_mint(self) -> EllipticCurvePrivateKey:
        try:
            pem = self._secrets.get(self._key_path)
        except SecretNotFoundError:
            key = ec.generate_private_key(_CURVE_FOR_ALG[self._alg]())
            pem = key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode("utf-8")
            self._secrets.put(self._key_path, pem)
            logger.warning(
                "JWT authority minted a new signing key",
                alg=self._alg,
                key_path=self._key_path,
            )
            return key
        loaded = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
        if not isinstance(loaded, EllipticCurvePrivateKey):
            raise ValueError(f"stored JWT signing key at {self._key_path!r} is not an EC key")
        logger.info("JWT authority loaded signing key", alg=self._alg)
        return loaded

    def sign(self, claims: dict[str, Any], *, expires_delta: timedelta | None = None) -> str:
        """Sign a DFE identity token. ``iss``/``iat``/``exp`` are set if absent."""
        payload = dict(claims)
        now = datetime.now(UTC)
        payload.setdefault("iss", self._issuer)
        payload.setdefault("iat", int(now.timestamp()))
        expire = now + (expires_delta or timedelta(minutes=self._expire_minutes))
        payload["exp"] = int(expire.timestamp())
        return jwt.encode(
            payload,
            self._private_key,
            algorithm=self._alg,
            headers={"kid": self._active_kid},
        )

    def mint_machine_token(
        self,
        *,
        audience: str,
        subject: str = MACHINE_SUBJECT,
        ttl_seconds: int = MACHINE_TOKEN_TTL_SECONDS,
    ) -> str:
        """Mint a short-lived service token for engine-to-peer control calls.

        Carries the team-admin role claim: this identity provisions the shipped
        dashboards, which dfe-hyperdx refuses to a token without it.

        Args:
            audience: The accepting peer's audience claim (e.g. ``dfe-hyperdx``).
            subject: Service subject identifying the caller.
            ttl_seconds: Token lifetime, hard-capped at ``MACHINE_TOKEN_TTL_SECONDS``.

        Returns:
            Signed JWT string, verifiable against the published JWKS.

        Raises:
            ValueError: If ``ttl_seconds`` is outside ``(0, MACHINE_TOKEN_TTL_SECONDS]``.
        """
        if not 0 < ttl_seconds <= MACHINE_TOKEN_TTL_SECONDS:
            raise ValueError(
                f"machine-token TTL must be in (0, {MACHINE_TOKEN_TTL_SECONDS}]s, got {ttl_seconds}"
            )
        return self.sign(
            {"sub": subject, "aud": audience, ROLE_CLAIM: SERVICE_ROLE},
            expires_delta=timedelta(seconds=ttl_seconds),
        )

    def verify(self, token: str) -> dict[str, Any]:
        """Verify signature + issuer + expiry. Raises ``InvalidTokenError`` on any failure."""
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        key = self._verify_keys.get(kid) if kid else None
        if key is None:
            raise InvalidTokenError(f"unknown or missing key id (kid={kid!r})")
        claims = jwt.decode(
            token,
            key,
            algorithms=[self._alg],
            issuer=self._issuer,
            leeway=_CLOCK_SKEW_SECONDS,
            options={"require": ["exp", "iss"], "verify_aud": False},
        )
        # Engine identity tokens carry no aud; an aud claim marks a token minted
        # for a peer service (e.g. dfe-hyperdx) and must not authenticate here.
        if "aud" in claims:
            raise InvalidTokenError(f"token audience {claims['aud']!r} is not for this issuer")
        return claims

    def jwks(self) -> dict[str, list[dict[str, str]]]:
        """The public JWK Set for ``/.well-known/jwks.json`` (public halves only)."""
        return {"keys": [_public_jwk(pub, self._alg) for pub in self._verify_keys.values()]}

    def add_verify_key_pem(self, public_pem: str) -> str:
        """Trust an additional public key for verification (rotation overlap). Returns its kid."""
        pub = serialization.load_pem_public_key(public_pem.encode("utf-8"))
        if not isinstance(pub, EllipticCurvePublicKey):
            raise ValueError("provided key is not an EC public key")
        kid = _public_jwk(pub, self._alg)["kid"]
        self._verify_keys[kid] = pub
        return kid


class MachineTokenSource:
    """Caching supplier of machine tokens: re-mints ~30s before expiry.

    Attributes are private; call :meth:`token` for a token guaranteed valid for
    at least ``_MACHINE_REFRESH_MARGIN_SECONDS``.
    """

    def __init__(
        self,
        authority: JwtAuthority,
        *,
        audience: str,
        subject: str = MACHINE_SUBJECT,
        ttl_seconds: int = MACHINE_TOKEN_TTL_SECONDS,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._authority = authority
        self._audience = audience
        self._subject = subject
        self._ttl = ttl_seconds
        # Injectable clock so cache-expiry behaviour is testable without sleeping.
        self._now = now or time.time
        self._token: str | None = None
        self._expires_at = 0.0

    def token(self) -> str:
        """Return a cached machine token, re-minting inside the refresh margin."""
        refresh_at = self._expires_at - _MACHINE_REFRESH_MARGIN_SECONDS
        if self._token is None or self._now() >= refresh_at:
            minted_at = self._now()
            self._token = self._authority.mint_machine_token(
                audience=self._audience,
                subject=self._subject,
                ttl_seconds=self._ttl,
            )
            self._expires_at = minted_at + self._ttl
        return self._token
