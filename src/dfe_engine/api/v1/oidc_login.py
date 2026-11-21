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
POST /api/v1/auth/oidc/link               -> link a held sign-in to its local account

A browser client (the console) passes ``return_to`` on login. The callback then
303s to it with the engine token in the URL fragment, which never leaves the
browser, or with the refusal's code there when it refuses the login. ``return_to``
must be a relative path or an origin the API already trusts for CORS, so a crafted
login link cannot send the token elsewhere.

The engine is the RP and the SINGLE token issuer: on callback it validates the
IdP id_token (via Authlib), extracts sub/email/groups, then mints its OWN ES384
token via ``JwtAuthority``. The engine token is returned in the JSON body AND set
as an httponly cookie so a browser flow can carry it forward. Downstream apps
only ever see the engine token - the IdP's token stays on the engine<->IdP leg.

These endpoints are deliberately unauthenticated - they ARE the login. Unknown
or disabled providers return 404.
"""

import functools
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Literal, NoReturn
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from jwt.exceptions import InvalidTokenError
from pydantic import BaseModel, Field, ValidationError
from scalo.api import ProblemError
from scalo.concurrency import run_blocking
from scalo.logger import logger

from dfe_engine.api.deps import (
    CurrentUser,
    Settings,
    _get_client_ip,
    bound_account,
    get_role_config,
    jwt_authority_for,
    require_local_account_enabled,
    resolve_live_grants_for_user,
    throttled,
)
from dfe_engine.auth import hyperdx_role
from dfe_engine.auth.audit import audit_jit_failed, audit_login_denied, audit_login_success
from dfe_engine.auth.identity_links import (
    LinkTicket,
    asserted_groups,
    linked_through,
    mint_link_ticket,
    read_link_ticket,
    trusted_email,
)
from dfe_engine.auth.jit import (
    JitAccountUnavailableError,
    JitEmailHeldError,
    JitEmailUntrustedError,
    JitIdentityCollisionError,
    JitLinkRequiredError,
    JitNoRoleError,
    JitSubjectUnusableError,
)
from dfe_engine.auth.reauth import (
    REAUTH_KINDS,
    REAUTH_LIFETIME,
    ReauthKind,
    grant_target,
    mint_reauth_grant,
)
from dfe_engine.auth.sessions import session_claims, token_lifetime
from dfe_engine.http_errors import describe_http_error
from dfe_engine.names import StoreName
from dfe_engine.settings import DFESettings, is_dev_posture

if TYPE_CHECKING:
    from dfe_engine.auth.accounts import Account
    from dfe_engine.auth.oidc.rp import NormalizedIdentity, OidcRelyingParty

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

# Session key holding a re-auth sign-in between its start and the callback.
_REAUTH_SESSION_KEY = "oidc_reauth"

# How long a re-auth sign-in may spend at the IdP.
_REAUTH_SIGN_IN_SECONDS = 600

# How far the IdP's clock may run behind the engine's when ``auth_time`` is judged.
_AUTH_TIME_SKEW_SECONDS = 30


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def validate_return_to(return_to: str, request_origin: str, trusted_origins: list[str]) -> str:
    r"""Accept a relative path or an absolute URL on a trusted origin, else raise 400.

    A relative path must start with a single ``/`` (``//host`` is scheme-relative
    and would leave the origin). An absolute URL must be http(s) on the API's own
    origin or on one of the configured CORS origins, which already name the
    browser clients allowed to talk to this API. A backslash, tab, CR or LF is
    refused anywhere, because a browser reads ``\`` as ``/`` and drops the others,
    which turns ``/\host`` or ``/<tab>/host`` into ``//host``.
    """
    if any(char in return_to for char in "\\\t\r\n"):
        _refuse_return_to()
    if return_to.startswith("/") and not return_to.startswith("//"):
        return return_to
    try:
        parts = urlsplit(return_to)
    except ValueError:  # a host that is invalid under NFKC, e.g. a full-width "/" or "@"
        _refuse_return_to()
    allowed = {request_origin.lower(), *(o.lower() for o in trusted_origins)}
    if parts.scheme in ("http", "https") and parts.netloc and _origin(return_to) in allowed:
        return return_to
    _refuse_return_to()


def _refuse_return_to() -> NoReturn:
    raise ProblemError(
        400,
        "return_to must be a relative path or a URL on a CORS-allowed origin",
        code="invalid_return_to",
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


class OidcReauthRequest(BaseModel):
    """The account or group you are about to change, and where the sign-in hands its grant back."""

    kind: ReauthKind = Field("account", description="What the target names")
    return_to: str = Field(
        description="Where the callback sends the browser, with the grant in the URL fragment "
        "(#reauth_grant=...&expires_in=...) or the refusal's code (#error=<code>&provider=...). "
        "A relative path, or an absolute URL on this API's origin or a CORS-allowed origin."
    )
    target: StoreName = Field(
        description="The account, group, role, sign-in provider or API key you are about to change"
    )


class OidcLinkRequest(BaseModel):
    """Body for linking a held IdP sign-in to its local account with that account's password."""

    password: str = Field(description="The local account's password")
    ticket: str = Field(description="The link_ticket a link_required callback answered with")


def _registered_state(request: Request, provider: str) -> bool | None:
    """Whether the provider registry holds *provider* enabled, or None when there is no registry.

    Costs one stat and one small YAML parse for the NAMED provider, with no
    directory walk. A file that does not load reads as not enabled.
    """
    registry = getattr(request.app.state, "oidc_provider_registry", None)
    if registry is None:
        return None
    try:
        known = registry.get(provider)
    except Exception as exc:  # malformed YAML on disk - not this request's problem
        # The validation text quotes the file's values, so only the failure's class is logged.
        logger.warning(
            "OIDC provider registry read failed", provider=provider, **describe_http_error(exc)
        )
        return False
    return known is not None and known.enabled


async def _rebuild_rp(request: Request):
    """Build the RP again from the registry, on a worker thread, and keep it on the app.

    The build reads each enabled provider's credentials from the secret store.
    """
    from dfe_engine.auth.oidc.rp import build_relying_party

    request.app.state.oidc_rp = await run_blocking(
        functools.partial(
            build_relying_party,
            request.app.state.oidc_provider_registry,
            secrets=getattr(request.app.state, "dfe_secrets", None),
            metrics=getattr(request.app.state, "oidc_login_metrics", None),
            directory_deadline_seconds=(
                request.app.state.settings.auth.oidc_login_directory_deadline_seconds
            ),
        )
    )
    return request.app.state.oidc_rp


async def _reload_rp_if_provider_known(request: Request, provider: str):
    """Rebuild the RP when the registry knows an enabled provider the RP does not.

    The provider-CRUD endpoints rebuild the RP on every write, but that only
    heals the worker that served the write. The registry is a directory of YAML
    files that also changes out-of-band - another worker or replica's write, a
    gitops sync, an operator editing a file - so the login path checks the
    registry once before giving up. An unauthenticated caller spamming bogus names
    buys itself a failed stat and nothing more.

    Returns the rebuilt RP, or None if the registry does not vindicate the miss.
    """
    if not _registered_state(request, provider):
        return None
    logger.info("OIDC RP stale - rebuilding for a provider added since startup", provider=provider)
    return await _rebuild_rp(request)


def _not_found(provider: str) -> ProblemError:
    return ProblemError(404, f"OIDC provider '{provider}' not found or disabled", code="not_found")


async def _rp_or_404(request: Request, provider: str):
    """Resolve the relying party and assert the registry still enables the provider.

    The RP is checked against the registry on every login, both ways: a provider
    added out-of-band rebuilds it, and one disabled or removed out-of-band rebuilds
    it without the provider and refuses.
    """
    rp = getattr(request.app.state, "oidc_rp", None)
    if rp is None or not rp.has_provider(provider):
        rp = await _reload_rp_if_provider_known(request, provider)
    if rp is None or not rp.has_provider(provider):
        raise _not_found(provider)
    if _registered_state(request, provider) is False:
        logger.info("OIDC provider disabled or removed out-of-band - rebuilding", provider=provider)
        await _rebuild_rp(request)
        raise _not_found(provider)
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
        "token in the URL fragment (#access_token=...&token_type=bearer&provider=...), "
        "or the refusal's code (#error=<code>&provider=...) when the login is refused. "
        "A relative path, or an absolute URL on this API's origin or a CORS-allowed "
        "origin. Omit to have the callback answer with JSON instead.",
    ),
) -> RedirectResponse | OidcLoginResponse:
    """Begin OIDC auth-code flow: 302 to the IdP, or JSON authorize URL for SPAs."""
    rp = await _rp_or_404(request, provider)
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


def _hand_back_url(return_to: str, fields: dict[str, str]) -> str:
    """``return_to`` with *fields* as its URL fragment, in place of any fragment it carried.

    The fragment reaches no server and no log line. The console reads it once.
    """
    return f"{return_to.partition('#')[0]}#{urlencode(fields)}"


def _hand_back_refusal(return_to: str, provider: str, exc: ProblemError) -> RedirectResponse:
    """303 a refused browser login to ``return_to`` with the refusal's code in the URL fragment.

    The code is one the engine wrote, never the IdP's text. No token or cookie goes with it.
    """
    fields = {"error": exc.problem.code or "unauthorized", "provider": provider}
    return RedirectResponse(_hand_back_url(return_to, fields), status_code=303)


@router.get(
    "/{provider}/callback",
    name="oidc_callback",
    response_model=OidcCallbackResponse,
    responses={
        303: {
            "description": "Login started with return_to: redirect there with the engine "
            "token in the URL fragment, or with the refusal's code "
            "(#error=<code>&provider=...) when the login is refused. A sign-in that "
            "links to a local account comes back with "
            "#error=link_required&link_ticket=...&account=...&provider=... instead."
        },
        409: {
            "description": "link_required: the sign-in's trusted email matches a local "
            "account. The body's link_ticket and that account's password go to POST "
            "/auth/oidc/link, and account names it."
        },
    },
)
async def oidc_callback(
    provider: str, request: Request, settings: Settings
) -> JSONResponse | RedirectResponse:
    """Complete the OIDC flow and re-mint the engine token (single issuer).

    A login started with ``return_to`` returns there refused as well as admitted,
    so a browser never lands on this route's JSON error. Without ``return_to`` a
    refusal answers JSON with its status, as any API error does.
    """
    # Spent before the provider check, so a 404 leaves no return_to behind either.
    pending = request.session.get(_REAUTH_SESSION_KEY)
    # Only the re-auth's own authorize request answers it: a login open in another tab stays a login.
    reauthing = (pending is not None) and (
        pending.get("state") == request.query_params.get("state")
    )
    if reauthing:
        request.session.pop(_REAUTH_SESSION_KEY, None)
        return_to = pending.get("return_to")
    else:
        return_to = request.session.pop(_RETURN_TO_SESSION_KEY, None)
    rp = await _rp_or_404(request, provider)
    try:
        if reauthing:
            return await _complete_reauth(
                pending=pending,
                provider=provider,
                request=request,
                return_to=return_to,
                rp=rp,
                settings=settings,
            )
        return await _complete_login(provider, request, settings, rp, return_to)
    except ProblemError as exc:
        if not return_to:
            raise
        return _hand_back_refusal(return_to, provider, exc)


async def _complete_login(
    provider: str,
    request: Request,
    settings: DFESettings,
    rp: OidcRelyingParty,
    return_to: str | None,
) -> JSONResponse | RedirectResponse:
    """Exchange the IdP code, admit the identity and mint its token. A refusal raises ProblemError."""
    try:
        identity = await rp.handle_callback(provider, request)
    except Exception as exc:
        # Bad code, failed id_token validation, nonce/state mismatch, etc. Authlib's text
        # repeats the callback's error_description query parameter, which any caller can set.
        failure = describe_http_error(exc)
        logger.warning("OIDC callback failed", provider=provider, **failure)
        # A refused credential is an audit event, not just an operational log line.
        reason = str(failure.get("code") or failure["error_type"])
        audit_login_denied("unknown", "oidc", _get_client_ip(request), reason)
        # The answer carries no reason: this route takes unauthenticated callers.
        raise ProblemError(
            401, "OIDC login failed; the engine log has the reason", code="unauthorized"
        ) from exc

    if not identity.subject:
        audit_login_denied("unknown", "oidc", _get_client_ip(request), "no_subject")
        raise ProblemError(401, "IdP id_token carried no subject", code="unauthorized")

    jit = getattr(request.app.state, "jit_provisioner", None)
    if jit:
        try:
            jit.ensure_account(
                identity.subject,
                identity.groups,
                provider,
                email=identity.email,
                name=identity.name,
                keep_stored_groups=identity.groups_unresolved,
                email_verified=_trusted_email(
                    identity=identity, provider=provider, request=request
                ),
            )
        except JitLinkRequiredError as exc:
            return _link_required(
                exc=exc,
                identity=identity,
                provider=provider,
                request=request,
                return_to=return_to,
                settings=settings,
            )
        except (JitIdentityCollisionError, JitSubjectUnusableError) as exc:
            # Ordered before the catch-all: a refused identity is a 401, not a 503.
            audit_login_denied(identity.subject, "oidc", _get_client_ip(request), exc.reason)
            raise ProblemError(401, str(exc), code="unauthorized") from exc
        except (
            JitAccountUnavailableError,
            JitEmailHeldError,
            JitEmailUntrustedError,
            JitNoRoleError,
        ) as exc:
            audit_login_denied(identity.subject, "oidc", _get_client_ip(request), exc.reason)
            raise ProblemError(401, str(exc), code=exc.reason) from exc
        except Exception as exc:
            # A token minted with no account behind it cannot be disabled locally. The text names store paths, so only its class is logged.
            logger.error(
                "JIT provisioning failed", user_id=identity.subject, **describe_http_error(exc)
            )
            audit_jit_failed(identity.subject, exc)
            raise ProblemError(
                503, "The login could not be recorded. Try again.", code="service_unavailable"
            ) from exc

    return _signed_in(
        email=identity.email,
        groups=identity.groups,
        groups_unresolved=identity.groups_unresolved,
        provider=provider,
        request=request,
        return_to=return_to,
        settings=settings,
        subject=identity.subject,
    )


async def _complete_reauth(
    *,
    pending: dict,
    provider: str,
    request: Request,
    return_to: str | None,
    rp: OidcRelyingParty,
    settings: DFESettings,
) -> JSONResponse | RedirectResponse:
    """Hand back a grant for a re-auth sign-in that is the caller's own and fresh. A refusal raises ProblemError."""
    caller = str(pending.get("caller") or "")
    started = int(pending.get("started") or 0)
    client_ip = _get_client_ip(request)
    if int(time.time()) - started > _REAUTH_SIGN_IN_SECONDS:
        audit_login_denied(caller, "reauth", client_ip, "reauth_expired")
        raise ProblemError(403, "The sign-in took too long. Try again.", code="reauth_expired")
    try:
        identity = await rp.handle_callback(provider, request)
    except Exception as exc:
        failure = describe_http_error(exc)
        logger.warning("OIDC re-auth callback failed", provider=provider, **failure)
        reason = str(failure.get("code") or failure["error_type"])
        audit_login_denied(caller, "reauth", client_ip, reason)
        raise ProblemError(
            403, "The sign-in failed; the engine log has the reason", code="reauth_failed"
        ) from exc
    account = require_local_account_enabled(request, caller)
    if (account is None) or ((provider, identity.subject) not in _sign_ins(account=account)):
        audit_login_denied(caller, "reauth", client_ip, "reauth_other_person")
        raise ProblemError(403, "That sign-in is not yours", code="reauth_other_person")
    if (identity.auth_time is None) or (identity.auth_time < started - _AUTH_TIME_SKEW_SECONDS):
        audit_login_denied(caller, "reauth", client_ip, "reauth_not_fresh")
        raise ProblemError(
            403,
            "Your identity provider did not ask for your credentials again",
            code="reauth_not_fresh",
        )
    kind = next((known for known in REAUTH_KINDS if known == pending.get("kind")), None)
    if kind is None:
        raise ProblemError(403, "The sign-in failed. Try again.", code="reauth_failed")
    target = grant_target(kind=kind, name=str(pending.get("target") or ""))
    grant = mint_reauth_grant(authority=jwt_authority_for(settings), caller=caller, target=target)
    expires_in = int(REAUTH_LIFETIME.total_seconds())
    if not (return_to):
        return JSONResponse({"expires_in": expires_in, "grant": grant})
    fields = {"expires_in": str(expires_in), "reauth_grant": grant}
    return RedirectResponse(_hand_back_url(return_to, fields), status_code=303)


def _link_required(
    *,
    exc: JitLinkRequiredError,
    identity: NormalizedIdentity,
    provider: str,
    request: Request,
    return_to: str | None,
    settings: DFESettings,
) -> RedirectResponse:
    """Hold a sign-in that links to a local account until that account's password, handing back its ticket.

    A browser login goes back to ``return_to`` with the ticket in the URL fragment beside ``error=link_required``, so a console that cannot link shows a refusal. Otherwise the 409 carries it.
    """
    link = LinkTicket(
        account=exc.account,
        email=identity.email,
        groups=identity.groups,
        groups_unresolved=identity.groups_unresolved,
        name=identity.name,
        provider=provider,
        subject=identity.subject,
    )
    ticket = mint_link_ticket(authority=jwt_authority_for(settings), ticket=link)
    audit_login_denied(identity.subject, "oidc", _get_client_ip(request), exc.reason)
    if not (return_to):
        raise ProblemError(409, str(exc), code=exc.reason, account=exc.account, link_ticket=ticket)
    fields = {
        "account": exc.account,
        "error": exc.reason,
        "link_ticket": ticket,
        "provider": provider,
    }
    return RedirectResponse(_hand_back_url(return_to, fields), status_code=303)


def _sign_ins(*, account: Account) -> set[tuple[str, str]]:
    """The ``(provider, subject)`` sign-ins that reach *account*: its own and each one linked to it."""
    held = {(identity.provider, identity.subject) for identity in account.identities}
    if (account.oidc_id) and (account.subject):
        held.add((account.oidc_id, account.subject))
    return held


def _trusted_email(*, identity: NormalizedIdentity, provider: str, request: Request) -> bool:
    """Whether the IdP vouched for the sign-in's email and the provider is trusted for its domain."""
    registry = getattr(request.app.state, "oidc_provider_registry", None)
    config = registry.get(provider) if registry is not None else None
    domains = config.trusted_email_domains if config is not None else []
    return trusted_email(
        email=identity.email, email_verified=identity.email_verified, trusted_domains=domains
    )


def _signed_in(
    *,
    email: str,
    groups: list[str],
    groups_unresolved: bool,
    provider: str,
    request: Request,
    return_to: str | None,
    settings: DFESettings,
    subject: str,
) -> JSONResponse | RedirectResponse:
    """Mint the engine token for the admitted IdP *subject* and answer with it.

    A sign-in linked to an account is that account's session, so the token's ``sub`` is the account's, which binds it back to the account on every request.
    """
    account = require_local_account_enabled(request, subject)
    linked = account is not None and linked_through(account=account, subject=subject)
    session_subject = account.session_subject if linked else subject
    # Groups the directory never answered for are the stored assertion, not none.
    if groups_unresolved and account is not None:
        groups = asserted_groups(account=account, subject=subject)

    # From the group files rather than the IdP token, for the claim and the audit.
    live = resolve_live_grants_for_user(request, subject)
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
            "sub": session_subject,
            "email": email,
            "groups": groups,
            # dfe-hyperdx gates changing what a team sees on this one value.
            hyperdx_role.CLAIM: hyperdx_role.role_claim(live.grants, get_role_config(request)),
            **session_claims(account, auth_time=now),
        },
        expires_delta=timedelta(seconds=lifetime),
        now=now,
    )

    logger.info(
        "OIDC login re-minted engine token",
        provider=provider,
        subject=subject,
        group_count=len(groups),
    )

    # The IdP code is exchanged for an engine token here, so this is the login
    # the audit trail counts - not the per-request token check in get_current_user.
    audit_login_success(session_subject, "oidc", _get_client_ip(request), roles)

    payload = OidcCallbackResponse(
        access_token=token,
        subject=subject,
        email=email,
        groups=groups,
    )
    response: JSONResponse | RedirectResponse
    if return_to:
        fields = {"access_token": token, "token_type": "bearer", "provider": provider}
        response = RedirectResponse(_hand_back_url(return_to, fields), status_code=303)
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


@router.post(
    "/link",
    response_model=OidcCallbackResponse,
    responses={
        401: {
            "description": "link_ticket_invalid (expired or not this engine's), a wrong "
            "password, link_stale (the account or the sign-in changed since the ticket), "
            "or the account is disabled, blocked or would hold no role"
        },
        404: {"description": "The ticket's provider is no longer registered and enabled"},
        429: {
            "description": "Too many failed attempts for this account or from this "
            "address; Retry-After gives the seconds to wait"
        },
    },
)
async def oidc_link(
    body: OidcLinkRequest, request: Request, settings: Settings
) -> JSONResponse | RedirectResponse:
    """Link the sign-in a ``link_required`` callback held to its local account, on that account's password, and sign it in.

    Wrong passwords count against the account's sign-in throttle (``auth.login_throttle``), as a password login's do. The answer is the callback's, and from then on the sign-in reaches the account without a password.
    """
    client_ip = _get_client_ip(request)
    try:
        ticket = read_link_ticket(authority=jwt_authority_for(settings), token=body.ticket)
    except (InvalidTokenError, ValidationError) as exc:
        audit_login_denied("unknown", "oidc", client_ip, "link_ticket_invalid")
        raise ProblemError(
            401,
            "The link has expired or is not valid. Sign in through your identity provider again.",
            code="link_ticket_invalid",
        ) from exc
    if not (_registered_state(request, ticket.provider)):
        raise _not_found(ticket.provider)
    domains = request.app.state.oidc_provider_registry.get(ticket.provider).trusted_email_domains
    if not (trusted_email(email=ticket.email, email_verified=True, trusted_domains=domains)):
        # Trust withdrawn since the ticket was issued.
        audit_login_denied(ticket.subject, "oidc", client_ip, "link_stale")
        raise ProblemError(401, "The link is no longer allowed. Sign in again.", code="link_stale")

    throttle = request.app.state.login_throttle
    wait = throttle.retry_after(ticket.account, client_ip)
    if wait:
        audit_login_denied(ticket.account, "oidc", client_ip, "throttled")
        raise throttled(request, wait)
    store = request.app.state.account_store
    account = store.get(ticket.account)
    verified = await run_blocking(store.verify_account_password, account, body.password)
    if (not (verified)) or (account is None) or (account.external):
        throttle.failed(ticket.account, client_ip)
        audit_login_denied(ticket.account, "oidc", client_ip, "invalid_password")
        raise ProblemError(401, "Invalid password", code="unauthorized")
    throttle.succeeded(ticket.account)

    jit = getattr(request.app.state, "jit_provisioner", None)
    if jit is None:
        raise _not_found(ticket.provider)
    try:
        jit.complete_link(ticket=ticket)
    except (JitIdentityCollisionError, JitSubjectUnusableError) as exc:
        audit_login_denied(ticket.subject, "oidc", client_ip, exc.reason)
        code = exc.reason if exc.reason == "link_stale" else "unauthorized"
        raise ProblemError(401, str(exc), code=code) from exc
    except (JitAccountUnavailableError, JitNoRoleError) as exc:
        audit_login_denied(ticket.subject, "oidc", client_ip, exc.reason)
        raise ProblemError(401, str(exc), code=exc.reason) from exc
    except Exception as exc:
        logger.error("Account link failed", user_id=ticket.subject, **describe_http_error(exc))
        audit_jit_failed(ticket.subject, exc)
        raise ProblemError(
            503, "The link could not be recorded. Try again.", code="service_unavailable"
        ) from exc

    return _signed_in(
        email=ticket.email,
        groups=ticket.groups,
        groups_unresolved=ticket.groups_unresolved,
        provider=ticket.provider,
        request=request,
        return_to=None,
        settings=settings,
        subject=ticket.subject,
    )


@router.post(
    "/reauth",
    response_model=OidcLoginResponse,
    response_model_exclude_none=True,
    responses={
        409: {
            "description": "reauth_via_password: the caller has no identity-provider sign-in, "
            "so it confirms with its password at POST /auth/reauth"
        }
    },
)
async def oidc_reauth(
    body: OidcReauthRequest, request: Request, settings: Settings, user: CurrentUser
) -> OidcLoginResponse:
    """Start a fresh sign-in at your own identity provider, which earns a grant for changing one account or group.

    The IdP is asked to take your credentials again (``prompt=login``, ``max_age=0``), and the callback answers with the grant instead of a session.
    """
    account = bound_account(request, user.user_id)
    held = _sign_ins(account=account) if account is not None else set()
    if not (held):
        raise ProblemError(
            409,
            "You have no identity-provider sign-in: confirm it is you with your password",
            code="reauth_via_password",
        )
    own = (account.oidc_id, account.subject)
    provider = own[0] if own in held else sorted(held)[0][0]
    rp = await _rp_or_404(request, provider)
    return_to = validate_return_to(
        body.return_to, _origin(str(request.base_url)), settings.api.cors_origins
    )
    redirect_uri = str(request.url_for("oidc_callback", provider=provider))
    url = await rp.login_authorization_url(
        provider, request, redirect_uri, max_age=0, prompt="login"
    )
    request.session[_REAUTH_SESSION_KEY] = {
        "caller": user.user_id,
        "kind": body.kind,
        "return_to": return_to,
        "started": int(time.time()),
        "state": parse_qs(urlsplit(url).query).get("state", [""])[0],
        "target": body.target,
    }
    return OidcLoginResponse(authorization_url=url)
