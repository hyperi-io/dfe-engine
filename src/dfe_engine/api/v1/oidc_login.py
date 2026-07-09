#  Project:      dfe-engine
#  File:         api/v1/oidc_login.py
#  Purpose:      OIDC RP login/callback - terminate IdP flow, re-mint engine token
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""OIDC relying-party login endpoints.

GET /api/v1/auth/oidc/{provider}/login     -> 302 to the IdP authorize endpoint
GET /api/v1/auth/oidc/{provider}/callback  -> exchange code, RE-MINT engine token

The engine is the RP and the SINGLE token issuer: on callback it validates the
IdP id_token (via Authlib), extracts sub/email/groups, then mints its OWN ES384
token via ``JwtAuthority``. The engine token is returned in the JSON body AND set
as an httponly cookie so a browser flow can carry it forward. Downstream apps
only ever see the engine token - the IdP's token stays on the engine<->IdP leg.

These endpoints are deliberately unauthenticated - they ARE the login. Unknown
or disabled providers return 404.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from scalo.logger import logger

from dfe_engine.api.deps import Settings, jwt_authority_for

router = APIRouter(prefix="/auth/oidc", tags=["OIDC Login"])

# Name of the cookie carrying the re-minted engine token to a browser client.
_TOKEN_COOKIE = "dfe_token"


def _rp_or_404(request: Request, provider: str):
    """Resolve the relying party and assert the provider is registered."""
    rp = getattr(request.app.state, "oidc_rp", None)
    if rp is None or not rp.has_provider(provider):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"OIDC provider '{provider}' not found or disabled",
            },
        )
    return rp


@router.get("/{provider}/login")
async def oidc_login(provider: str, request: Request):
    """Redirect the user agent to the IdP to begin the OIDC auth-code flow."""
    rp = _rp_or_404(request, provider)
    # Callback URL is built from this request's base URL so it works behind any
    # ingress without a hardcoded host. Must match a redirect URI the IdP allows.
    redirect_uri = str(request.url_for("oidc_callback", provider=provider))
    return await rp.login_redirect(provider, request, redirect_uri)


@router.get("/{provider}/callback", name="oidc_callback")
async def oidc_callback(provider: str, request: Request, settings: Settings):
    """Complete the OIDC flow and re-mint the engine token (single issuer)."""
    rp = _rp_or_404(request, provider)

    try:
        identity = await rp.handle_callback(provider, request)
    except Exception as exc:
        # Bad code, failed id_token validation, nonce/state mismatch, etc.
        logger.warning("OIDC callback failed", provider=provider, error=str(exc))
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": f"OIDC login failed: {exc}"},
        )

    if not identity.subject:
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": "IdP id_token carried no subject"},
        )

    # RE-MINT: the engine's own ES384 identity token is the ONLY token downstream
    # apps ever see. iss/iat/exp are set by the authority.
    token = jwt_authority_for(settings).sign(
        {
            "sub": identity.subject,
            "email": identity.email,
            "groups": identity.groups,
        }
    )

    logger.info(
        "OIDC login re-minted engine token",
        provider=provider,
        subject=identity.subject,
        group_count=len(identity.groups),
    )

    response = JSONResponse(
        {
            "access_token": token,
            "token_type": "bearer",
            "subject": identity.subject,
            "email": identity.email,
            "groups": identity.groups,
        }
    )
    response.set_cookie(
        _TOKEN_COOKIE,
        token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=settings.api.jwt_expire_minutes * 60,
    )
    return response
