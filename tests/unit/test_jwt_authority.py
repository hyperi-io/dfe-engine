#  Project:      dfe-engine
#  File:         tests/unit/test_jwt_authority.py
#  Purpose:      The ES384 JWT authority - real scalo.secrets file backend, no mocks
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The DFE JWT authority: ES384 sign/verify, JWKS, key persistence, tamper + expiry."""

from __future__ import annotations

from datetime import timedelta

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from jwt.exceptions import InvalidTokenError

from dfe_engine.auth import hyperdx_role
from dfe_engine.auth.jwt_authority import (
    HYPERDX_AUDIENCE,
    MACHINE_SUBJECT,
    MACHINE_TOKEN_TTL_SECONDS,
    JwtAuthority,
    MachineTokenSource,
)
from dfe_engine.secrets import build_secrets
from dfe_engine.settings import SecretsSettings

ISS = "https://dfe.test/api"


def _authority(path, issuer: str = ISS, **kw) -> JwtAuthority:
    secrets = build_secrets(SecretsSettings(provider="file", path=str(path)))
    return JwtAuthority(secrets, issuer=issuer, **kw)


def test_sign_verify_roundtrip(tmp_path):
    a = _authority(tmp_path)
    token = a.sign({"sub": "alice@acme", "groups": ["soc"]})
    claims = a.verify(token)
    assert claims["sub"] == "alice@acme"
    assert claims["groups"] == ["soc"]
    assert claims["iss"] == ISS
    assert "exp" in claims
    assert "iat" in claims


def test_token_header_is_es384_with_kid(tmp_path):
    a = _authority(tmp_path)
    token = a.sign({"sub": "x"})
    header = pyjwt.get_unverified_header(token)
    assert header["alg"] == "ES384"
    assert header["kid"] == a.kid


def test_jwks_is_public_only_and_matches_kid(tmp_path):
    a = _authority(tmp_path)
    jwks = a.jwks()
    assert len(jwks["keys"]) == 1
    key = jwks["keys"][0]
    assert key["kty"] == "EC"
    assert key["crv"] == "P-384"
    assert key["use"] == "sig"
    assert key["alg"] == "ES384"
    assert key["kid"] == a.kid
    # no private scalar must ever appear in the JWKS
    assert "d" not in key


def test_key_persists_across_restarts(tmp_path):
    first = _authority(tmp_path)
    kid = first.kid
    token = first.sign({"sub": "bob"})
    # a fresh authority over the same secrets store must reload the same key
    second = _authority(tmp_path)
    assert second.kid == kid
    assert second.verify(token)["sub"] == "bob"


def test_tampered_payload_rejected(tmp_path):
    a = _authority(tmp_path)
    token = a.sign({"sub": "eve", "groups": ["admin"]})
    head, payload, sig = token.split(".")
    swapped = "AAAA" if payload[:4] != "AAAA" else "BBBB"
    tampered = f"{head}.{swapped}{payload[4:]}.{sig}"
    with pytest.raises(InvalidTokenError):
        a.verify(tampered)


def test_expired_token_rejected(tmp_path):
    a = _authority(tmp_path)
    token = a.sign({"sub": "x"}, expires_delta=timedelta(seconds=-120))
    with pytest.raises(InvalidTokenError):
        a.verify(token)


def test_wrong_issuer_rejected(tmp_path):
    # same key store (so the kid is known), different expected issuer
    a = _authority(tmp_path, issuer="https://dfe.test/api")
    token = a.sign({"sub": "x"})
    b = _authority(tmp_path, issuer="https://impostor/api")
    with pytest.raises(InvalidTokenError):
        b.verify(token)


def test_unknown_kid_rejected(tmp_path):
    a = _authority(tmp_path / "a")
    token = a.sign({"sub": "x"})
    b = _authority(tmp_path / "b")  # different key -> different kid
    with pytest.raises(InvalidTokenError):
        b.verify(token)


def test_rotation_overlap_verifies_old_and_new(tmp_path):
    old = _authority(tmp_path / "old")
    old_token = old.sign({"sub": "x"})
    new = _authority(tmp_path / "new")
    # publish the old public key into the new authority (rotation overlap)
    old_pub_pem = (
        old._private_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    added_kid = new.add_verify_key_pem(old_pub_pem)
    assert added_kid == old.kid
    assert new.verify(old_token)["sub"] == "x"  # old token still verifies
    assert new.verify(new.sign({"sub": "y"}))["sub"] == "y"  # new token too
    assert {k["kid"] for k in new.jwks()["keys"]} == {old.kid, new.kid}


def test_hs256_rejected_cannot_back_jwks(tmp_path):
    secrets = build_secrets(SecretsSettings(provider="file", path=str(tmp_path)))
    with pytest.raises(ValueError):
        JwtAuthority(secrets, issuer=ISS, algorithm="HS256")


# --- machine tokens (engine -> dfe-hyperdx control calls, dfe-engine#149) ---


def test_machine_token_verifies_against_published_jwks(tmp_path):
    a = _authority(tmp_path)
    token = a.mint_machine_token(audience=HYPERDX_AUDIENCE)
    # the fork trusts ONLY the published JWKS, so verify through that path
    jwk = a.jwks()["keys"][0]
    claims = pyjwt.decode(
        token,
        pyjwt.PyJWK(jwk).key,
        algorithms=["ES384"],
        audience=HYPERDX_AUDIENCE,
        issuer=ISS,
    )
    assert claims["sub"] == MACHINE_SUBJECT
    assert claims["aud"] == HYPERDX_AUDIENCE
    assert pyjwt.get_unverified_header(token)["kid"] == jwk["kid"]


def _decode_via_jwks(a: JwtAuthority, token: str, audience: str) -> dict:
    """Decode a token the way a peer does - through the published JWKS."""
    jwk = a.jwks()["keys"][0]
    return pyjwt.decode(
        token,
        pyjwt.PyJWK(jwk).key,
        algorithms=["ES384"],
        audience=audience,
        issuer=ISS,
    )


def test_machine_token_ttl_capped_at_300(tmp_path):
    a = _authority(tmp_path)
    token = a.mint_machine_token(audience=HYPERDX_AUDIENCE)
    claims = _decode_via_jwks(a, token, HYPERDX_AUDIENCE)
    assert claims["exp"] - claims["iat"] <= MACHINE_TOKEN_TTL_SECONDS


@pytest.mark.parametrize("ttl", [0, -1, MACHINE_TOKEN_TTL_SECONDS + 1])
def test_machine_token_ttl_out_of_range_rejected(tmp_path, ttl):
    a = _authority(tmp_path)
    with pytest.raises(ValueError):
        a.mint_machine_token(audience=HYPERDX_AUDIENCE, ttl_seconds=ttl)


def test_machine_token_wrong_audience_rejected_by_verifier(tmp_path):
    a = _authority(tmp_path)
    token = a.mint_machine_token(audience="someone-else")
    jwk = a.jwks()["keys"][0]
    with pytest.raises(InvalidTokenError):
        pyjwt.decode(
            token,
            pyjwt.PyJWK(jwk).key,
            algorithms=["ES384"],
            audience=HYPERDX_AUDIENCE,
            issuer=ISS,
        )


def test_machine_token_carries_the_team_admin_role_claim(tmp_path):
    # The engine provisions the shipped dashboards, which the fork refuses to a
    # token whose role claim is not one it allows (dfe-engine#387).
    a = _authority(tmp_path)
    claims = _decode_via_jwks(a, a.mint_machine_token(audience=HYPERDX_AUDIENCE), HYPERDX_AUDIENCE)
    assert claims[hyperdx_role.CLAIM] == hyperdx_role.TEAM_ADMIN
    assert claims[hyperdx_role.CLAIM] in hyperdx_role.FORK_ACCEPTS


def test_machine_token_source_caches_and_refreshes_near_expiry(tmp_path):
    a = _authority(tmp_path)
    clock = {"t": 1_000_000.0}
    src = MachineTokenSource(a, audience=HYPERDX_AUDIENCE, now=lambda: clock["t"])
    first = src.token()
    clock["t"] += 60.0
    assert src.token() is first  # still cached mid-life
    clock["t"] = 1_000_000.0 + MACHINE_TOKEN_TTL_SECONDS - 29.0
    assert src.token() is not first  # inside the 30s margin -> re-minted


def test_machine_token_source_token_is_valid(tmp_path):
    a = _authority(tmp_path)
    src = MachineTokenSource(a, audience=HYPERDX_AUDIENCE)
    claims = _decode_via_jwks(a, src.token(), HYPERDX_AUDIENCE)
    assert claims["sub"] == MACHINE_SUBJECT
    assert claims["aud"] == HYPERDX_AUDIENCE


def test_machine_token_rejected_by_engine_verify(tmp_path):
    # a peer-audience token must never authenticate against the engine itself
    a = _authority(tmp_path)
    token = a.mint_machine_token(audience=HYPERDX_AUDIENCE)
    with pytest.raises(InvalidTokenError):
        a.verify(token)


def test_verify_still_accepts_identity_tokens_without_aud(tmp_path):
    a = _authority(tmp_path)
    token = a.sign({"sub": "alice@acme", "groups": ["soc"]})
    assert a.verify(token)["sub"] == "alice@acme"
