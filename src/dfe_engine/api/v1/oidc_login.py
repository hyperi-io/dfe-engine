#  Project:      dfe-engine
#  File:         api/v1/oidc_login.py
#  Purpose:      OIDC RP login/callback - terminate IdP flow, re-mint engine token
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""OIDC relying-party login endpoints.

GET /api/v1/auth/oidc/{provider}/login     -> 302 to the IdP authorize endpoint
GET /api/v1/auth/oidc/{provider}/login?redirect=false -> JSON {authorization_url}
GET /api/v1/auth/oidc/{provider}/login?return_to=<url> -> callback hands the browser back there
GET /api/v1/auth/oidc/{provider}/callback  -> exchange code, RE-MINT engine token

A browser client (the console) passes ``return_to`` on login; the callback then
303s to it with the engine token in the URL fragment, which never leaves the
browser. ``return_to`` must be a relative path or an origin the API already
trusts for CORS, so a crafted login link cannot send the token elsewhere.

The engine is the RP and the SINGLE token issuer: on callback it validates the
IdP id_token (via Authlib), extracts sub/email/groups, then mints its OWN ES384
token via ``JwtAuthority``. The engine token is returned in the JSON body AND set
as an httponly cookie so a browser flow can carry it forward. Downstream apps
only ever see the engine token - the IdP's token stays on the engine<->IdP leg.

These endpoints are deliberately unauthenticated - they ARE the login. Unknown
or disabled providers return 404.
"""

import time
from datetime import timedelta
from typing import Literal
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from scalo.logger import logger

from dfe_engine.api.deps import (
    Settings,
    _get_client_ip,
    jwt_authority_for,
    require_local_account_enabled,
    resolve_live_grants_for_user,
)
from dfe_engine.auth import hyperdx_role
from dfe_engine.auth.audit import audit_jit_failed, audit_login_denied, audit_login_success
from dfe_engine.auth.jit import (
    JitAccountUnavailableError,
    JitIdentityCollisionError,
    JitSubjectUnusableError,
)
from dfe_engine.auth.oidc.idp_errors import describe_idp_error
from dfe_engine.auth.sessions import session_claims, token_lifetime
from dfe_engine.settings import DFESettings, is_dev_posture

router = APIRouter(prefix="/auth/oidc", tags=["OIDC Login"])

# Name of the cookie carrying the re-minted engine token to a browser client.
TOKEN_COOKIE = "dfe_token"  # noqa: S105, RUF100 - a cookie name


def token_cookie_secure(settings: DFESettings) -> bool:
    """Whether the token cookie carries ``Secure``, on the posture the session cookie follows.

    A browser drops a ``Secure`` cookie set over plain http, so a dev stack served
    without TLS would complete the login and keep no token.
    """
    return not is_dev_posture(settings.env)


# Session key holding the validated return_to between login and callback.
_RETURN_TO_SESSION_KEY = "oidc_return_to"


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def validate_return_to(return_to: str, request_origin: str, trusted_origins: list[str]) -> str:
    """Accept a relative path or an absolute URL on a trusted origin; raise 400 otherwise.

    A relative path must start with a single ``/`` (``//host`` is scheme-relative
    and would leave the origin). An absolute URL must be http(s) on the API's own
    origin or on one of the configured CORS origins, which already name the
    browser clients allowed to talk to this API.
    """
    if return_to.startswith("/") and not return_to.startswith("//"):
        return return_to
    parts = urlsplit(return_to)
    allowed = {request_origin.lower(), *(o.lower() for o in trusted_origins)}
    if parts.scheme in ("http", "https") and parts.netloc and _origin(return_to) in allowed:
        return return_to
    raise HTTPException(
        status_code=400,
        detail={
            "code": "invalid_return_to",
            "message": "return_to must be a relative path or a URL on a CORS-allowed origin",
        },
    )


class OidcLoginResponse(BaseModel):
    """JSON body for GET .../login when ``redirect=false``."""

    authorization_url: str | None = Field(
        default=None,
        description="IdP authorize URL for SPA clients (redirect=false). Omitted when "
        "redirect=true (302 to the IdP instead).",
    )


class OidcCallbackResponse(BaseModel):
    """Engine token minted after a successful IdP callback."""

    access_token: str = Field(description="Engine JWT Bearer token")
    token_type: Literal["bearer"] = "bearer"  # noqa: S105, RUF100 - the OAuth token type
    subject: str = Field(description="IdP subject (sub)")
    email: str = Field(default="", description="Email from the IdP, if asserted")
    groups: list[str] = Field(default_factory=list, description="Resolved group identifiers")


def _reload_rp_if_provider_known(request: Request, provider: str):
    """Rebuild the RP when the registry knows an enabled provider the RP does not.

    The provider-CRUD endpoints rebuild the RP on every write, but that only
    heals the worker that served the write. The registry is a directory of YAML
    files that also changes out-of-band - another uvicorn worker's write, a
    gitops sync, an operator editing a file - so the login path checks the
    registry once before giving up. That turns "restart the engine" into a
    self-heal.

    Costs a single stat + one small YAML parse for the NAMED provider (no
    directory walk), so an unauthenticated caller spamming bogus names buys
    itself a failed stat and nothing more.

    Returns the rebuilt RP, or None if the registry does not vindicate the miss.
    """
    registry = getattr(request.app.state, "oidc_provider_registry", None)
    if registry is None:
        return None
    try:
        known = registry.get(provider)
    except Exception as exc:  # malformed YAML on disk - not this request's problem
        logger.warning("OIDC provider registry read failed", provider=provider, error=str(exc))
        return None
    if known is None or not known.enabled:
        return None

    from dfe_engine.auth.oidc.rp import build_relying_party

    logger.info("OIDC RP stale - rebuilding for a provider added since startup", provider=provider)
    request.app.state.oidc_rp = build_relying_party(
        registry, secrets=getattr(request.app.state, "dfe_secrets", None)
    )
    return request.app.state.oidc_rp


def _rp_or_404(request: Request, provider: str):
    """Resolve the relying party and assert the provider is registered."""
    rp = getattr(request.app.state, "oidc_rp", None)
    if rp is None or not rp.has_provider(provider):
        rp = _reload_rp_if_provider_known(request, provider)
    if rp is None or not rp.has_provider(provider):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"OIDC provider '{provider}' not found or disabled",
            },
        )
    return rp


@router.get(
    "/{provider}/login",
    response_model=OidcLoginResponse,
    response_model_exclude_none=True,
    responses={302: {"description": "Redirect to the IdP authorize URL (redirect=true, default)."}},
)
async def oidc_login(
    provider: str,
    request: Request,
    settings: Settings,
    redirect: bool = Query(
        True,
        description="When false, return JSON with authorization_url for SPA clients "
        "(use credentials: include, then window.location.assign the URL).",
    ),
    return_to: str | None = Query(
        None,
        description="Where the callback sends the browser after login, with the engine "
        "token in the URL fragment (#access_token=...&token_type=bearer&provider=...). "
        "A relative path, or an absolute URL on this API's origin or a CORS-allowed "
        "origin. Omit to have the callback answer with JSON instead.",
    ),
) -> RedirectResponse | OidcLoginResponse:
    """Begin OIDC auth-code flow: 302 to the IdP, or JSON authorize URL for SPAs."""
    rp = _rp_or_404(request, provider)
    if return_to:
        request.session[_RETURN_TO_SESSION_KEY] = validate_return_to(
            return_to, _origin(str(request.base_url)), settings.api.cors_origins
        )
    else:
        request.session.pop(_RETURN_TO_SESSION_KEY, None)
    # Callback URL is built from this request's base URL so it works behind any
    # ingress without a hardcoded host. Must match a redirect URI the IdP allows.
    redirect_uri = str(request.url_for("oidc_callback", provider=provider))
    if not redirect:
        url = await rp.login_authorization_url(provider, request, redirect_uri)
        return OidcLoginResponse(authorization_url=url)
    return await rp.login_redirect(provider, request, redirect_uri)


@router.get(
    "/{provider}/callback",
    name="oidc_callback",
    response_model=OidcCallbackResponse,
    responses={
        303: {
            "description": "Login started with return_to: redirect there with the engine "
            "token in the URL fragment."
        }
    },
)
async def oidc_callback(
    provider: str, request: Request, settings: Settings
) -> JSONResponse | RedirectResponse:
    """Complete the OIDC flow and re-mint the engine token (single issuer)."""
    rp = _rp_or_404(request, provider)
    return_to = request.session.pop(_RETURN_TO_SESSION_KEY, None)

    try:
        identity = await rp.handle_callback(provider, request)
    except Exception as exc:
        # Bad code, failed id_token validation, nonce/state mismatch, etc. Authlib's text
        # repeats the callback's error_description query parameter, which any caller can set.
        failure = describe_idp_error(exc)
        logger.warning("OIDC callback failed", provider=provider, **failure)
        # A refused credential is an audit event, not just an operational log line.
        reason = str(failure.get("code") or failure["error_type"])
        audit_login_denied("unknown", "oidc", _get_client_ip(request), reason)
        # The answer carries no reason: this route takes unauthenticated callers.
        raise HTTPException(
            status_code=401,
            detail={
                "code": "unauthorized",
                "message": "OIDC login failed; the engine log has the reason",
            },
        ) from exc

    if not identity.subject:
        audit_login_denied("unknown", "oidc", _get_client_ip(request), "no_subject")
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": "IdP id_token carried no subject"},
        )

    jit = getattr(request.app.state, "jit_provisioner", None)
    if jit:
        try:
            jit.ensure_account(
                identity.subject,
                identity.groups,
                provider,
                email=identity.email,
                name=identity.name,
            )
        except (JitIdentityCollisionError, JitSubjectUnusableError) as exc:
            # Ordered before the catch-all: a refused identity is a 401, not a 503.
            audit_login_denied(identity.subject, "oidc", _get_client_ip(request), exc.reason)
            raise HTTPException(
                status_code=401,
                detail={"code": "unauthorized", "message": str(exc)},
            ) from exc
        except JitAccountUnavailableError as exc:
            audit_login_denied(identity.subject, "oidc", _get_client_ip(request), exc.reason)
            raise HTTPException(
                status_code=401,
                detail={"code": exc.reason, "message": str(exc)},
            ) from exc
        except Exception as exc:
            # A token minted with no account behind it cannot be disabled locally.
            logger.exception("JIT provisioning failed", user_id=identity.subject)
            audit_jit_failed(identity.subject, repr(exc))
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "service_unavailable",
                    "message": "The login could not be recorded. Try again.",
                },
            ) from exc

    account = require_local_account_enabled(request, identity.subject)

    # From the group files rather than the IdP token, for the claim and the audit.
    live = resolve_live_grants_for_user(request, identity.subject)
    roles = live.roles

    # RE-MINT: the engine's own ES384 identity token is the ONLY token downstream
    # apps ever see. iss/iat/exp are set by the authority.
    now = int(time.time())
    lifetime = token_lifetime(
        now,
        now=now,
        expire_minutes=settings.api.jwt_expire_minutes,
        max_session_minutes=settings.api.max_session_minutes,
    )
    token = jwt_authority_for(settings).sign(
        {
            "sub": identity.subject,
            "email": identity.email,
            "groups": identity.groups,
            # dfe-hyperdx gates changing what a team sees on this one value.
            hyperdx_role.CLAIM: hyperdx_role.role_claim(live.grants),
            **session_claims(account, auth_time=now),
        },
        expires_delta=timedelta(seconds=lifetime),
        now=now,
    )

    logger.info(
        "OIDC login re-minted engine token",
        provider=provider,
        subject=identity.subject,
        group_count=len(identity.groups),
    )

    # The IdP code is exchanged for an engine token here, so this is the login
    # the audit trail counts - not the per-request token check in get_current_user.
    audit_login_success(identity.subject, "oidc", _get_client_ip(request), roles)

    payload = OidcCallbackResponse(
        access_token=token,
        subject=identity.subject,
        email=identity.email,
        groups=identity.groups,
    )
    response: JSONResponse | RedirectResponse
    if return_to:
        # The fragment never reaches a server or a log line; the console reads it once.
        fragment = urlencode({"access_token": token, "token_type": "bearer", "provider": provider})
        response = RedirectResponse(f"{return_to}#{fragment}", status_code=303)
    else:
        response = JSONResponse(payload.model_dump())
    response.set_cookie(
        TOKEN_COOKIE,
        token,
        httponly=True,
        secure=token_cookie_secure(settings),
        samesite="lax",
        max_age=lifetime,
    )
    return response
