#  Project:      dfe-engine
#  File:         api/well_known.py
#  Purpose:      JWKS + OIDC discovery - the engine as the JWT authority
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Well-known discovery endpoints for the DFE JWT authority.

Publishes the public JWK Set at ``/.well-known/jwks.json`` so every peer - Envoy
``jwt_authn``, dfe-ui, dfe-hyperdx - verifies the DFE ES384 identity token by
signature, plus a minimal OIDC discovery document pointing at it. These are public
(unauthenticated) by design: they expose only public keys and metadata.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(tags=["well-known"])


@router.get("/.well-known/jwks.json", include_in_schema=False)
async def jwks(request: Request) -> dict:
    """The public JWK Set for verifying DFE-issued tokens (public keys only)."""
    return request.app.state.jwt_authority.jwks()


@router.get("/.well-known/openid-configuration", include_in_schema=False)
async def openid_configuration(request: Request) -> dict:
    """Minimal OIDC discovery document advertising the issuer + JWKS URI."""
    authority = request.app.state.jwt_authority
    base = str(request.base_url).rstrip("/")
    return {
        "issuer": authority.issuer,
        "jwks_uri": f"{base}/.well-known/jwks.json",
        "id_token_signing_alg_values_supported": [authority.algorithm],
        "response_types_supported": ["token"],
        "subject_types_supported": ["public"],
    }
