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

from dfe_engine.auth.jwt_authority import JwtAuthority
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
