#  Project:      dfe-engine
#  File:         api/v1/ingest_credentials.py
#  Purpose:      An org's ingest credentials: the bearer tokens dfe-receiver holds to orgs
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Ingest credentials, owned by an org and nested under it.

GET    /api/v1/orgs/{org}/ingest-credentials                       -> list
GET    /api/v1/orgs/{org}/ingest-credentials/{credential}          -> get
PUT    /api/v1/orgs/{org}/ingest-credentials/{credential}          -> apply
DELETE /api/v1/orgs/{org}/ingest-credentials/{credential}          -> delete
POST   /api/v1/orgs/{org}/ingest-credentials/{credential}/rotate   -> rotate

A credential is a bearer token a sender presents to dfe-receiver, and the orgs its
records may land in. The receiver holds each record's ``_org_id`` to them, so a
record lands only in an org its credential binds. ``apply`` creates with
``If-None-Match: *`` and mints the token, which comes back in that response and
never again, or updates the binding with ``If-Match``. ``rotate`` mints a new token
in place of the old one. Every mutation takes ``dry_run``. A mutation another write
gets to first changes nothing. It answers 412 where its precondition no longer holds,
else 409 ``conflict``, and may be sent again as it is.

Reads need ``org:read`` at the owning org's scope. Writes need ``org:write`` there,
and at the scope of every other org the credential binds, because a credential
writes into each of them.
"""

import functools
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from scalo.api import (
    DryRun,
    FieldError,
    ListParams,
    Page,
    PreconditionHeaders,
    Preconditions,
    ProblemError,
    etag_of,
    paginate,
)
from scalo.concurrency import run_blocking
from scalo.logger import logger
from scalo.secrets.exceptions import SecretsError

from dfe_engine.api.deps import CurrentUser, check_action, require_org_action
from dfe_engine.api.errors import ErrorCode, backend_failure
from dfe_engine.api.v1.orgs import reconcile_tenancy
from dfe_engine.api.write_turn import WRITE_TURN
from dfe_engine.auth import Scope
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.orgs.ingest_credentials import (
    BEARER_LISTENERS,
    CREDENTIAL_ID_PATTERN,
    CredentialChangedError,
    CredentialIdTakenError,
    IngestCredential,
    IngestListener,
)
from dfe_engine.orgs.ingest_tokens import (
    BearerSourceUnreadableError,
    bearer_secret_source,
    credential_secret_path,
    drop_token,
    mint_token,
    new_token_id,
    store_token,
    sync_bearer_tokens,
)
from dfe_engine.orgs.registry import OrgRegistry
from dfe_engine.secrets import DfeSecrets
from dfe_engine.settings import SecretsSettings, get_settings

router = APIRouter(
    prefix="/orgs/{org}/ingest-credentials",
    tags=["Ingest credentials"],
    dependencies=[WRITE_TURN],
)

_READ = Depends(require_org_action(scopes_dict["org_read"]))
_WRITE = Depends(require_org_action(scopes_dict["org_write"]))

_RESOURCE = "ingest_credential"

_TOKEN_DESCRIPTION = (
    "The bearer token a sender presents as `Authorization: Bearer <token>`. Returned "
    "once, by the request that mints it, and never again"
)


# -- Request / Response models -----------------------------------------------


class IngestCredentialSpec(BaseModel):
    """What an operator sets on an ingest credential."""

    model_config = ConfigDict(extra="forbid")

    orgs: list[str] | None = Field(
        default=None,
        description="The orgs its records may land in, the owning org among them. Unset is "
        "the owning org alone",
    )
    default_org: str | None = Field(
        default=None,
        description="The org a record takes when no rule picks one of orgs. Unset is the "
        "owning org",
    )
    listeners: list[IngestListener] = Field(
        default_factory=lambda: list(BEARER_LISTENERS),
        min_length=1,
        description="The receiver listeners that accept its token. Unset is every one",
    )


class IngestCredentialResponse(BaseModel):
    """An ingest credential, never its token."""

    name: str = Field(description="The credential id, unique across every org")
    org: str = Field(description="The org that owns it")
    orgs: list[str] = Field(description="The orgs its records may land in")
    default_org: str = Field(description="The org a record takes when no rule picks one")
    listeners: list[IngestListener] = Field(description="The listeners that accept its token")
    created_at: str = Field(description="When its first token was minted (ISO 8601)")
    rotated_at: str = Field(description="When its current token was minted (ISO 8601)")


class IngestCredentialWriteResponse(IngestCredentialResponse):
    """The credential a write leaves, and the token when this write minted one."""

    token: str | None = Field(default=None, description=_TOKEN_DESCRIPTION)
    dry_run: bool = Field(description="Nothing was written: this is what the write would do")
    apps_synced: list[str] = Field(
        default_factory=list, description="The app overlays the binding change rewrote"
    )
    apps_sync_error: str | None = Field(
        default=None, description="Why the app overlays were not brought into step, if not"
    )


# -- Helpers -----------------------------------------------------------------


def _registry(request: Request) -> OrgRegistry:
    return request.app.state.org_registry


def _secrets(request: Request) -> DfeSecrets:
    return request.app.state.dfe_secrets


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _require_org(registry: OrgRegistry, org: str) -> None:
    """Raises: HTTPException: 404 ``not_found`` when no org is named *org*."""
    if registry.get(org) is None:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": f"Org '{org}' not found"}
        )


def _owned(registry: OrgRegistry, org: str, name: str) -> IngestCredential | None:
    """The credential *name* when *org* owns it. One another org owns reads as absent here."""
    held = registry.ingest_credentials.get(name)
    return held if held is not None and held.org == org else None


def _require_owned(registry: OrgRegistry, org: str, name: str) -> IngestCredential:
    """Raises: HTTPException: 404 ``not_found`` unless *org* owns a credential *name*."""
    held = _owned(registry, org, name)
    if held is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "not_found",
                "message": f"Ingest credential '{name}' not found in org '{org}'",
            },
        )
    return held


def credential_etag(credential: IngestCredential) -> str:
    """The credential's ETag, over the fields an operator sets."""
    return etag_of(credential.content())


def _response(credential: IngestCredential) -> IngestCredentialResponse:
    return IngestCredentialResponse.model_validate(credential.model_dump())


def _written(
    credential: IngestCredential,
    *,
    dry_run: bool,
    token: str | None = None,
    synced: Any = None,
) -> IngestCredentialWriteResponse:
    return IngestCredentialWriteResponse(
        **credential.model_dump(),
        token=token,
        dry_run=dry_run,
        apps_synced=list(synced.changes) if synced is not None else [],
        apps_sync_error=synced.error if synced is not None else None,
    )


def _field(field: str, detail: str) -> FieldError:
    return FieldError(field=field, location="body", detail=detail, code="invalid_binding")


def _desired(
    registry: OrgRegistry,
    org: str,
    name: str,
    spec: IngestCredentialSpec,
    backend: SecretsSettings,
) -> IngestCredential:
    """The credential *spec* describes, refused where the receiver could not hold it.

    Raises:
        ProblemError: 422 ``invalid_binding`` with one field error per problem: an
            org that is not registered or is listed twice, an ``orgs`` without the
            owning org, a ``default_org`` outside ``orgs``, a listener listed twice,
            or listeners on a secrets *backend* the receiver cannot read its tokens from.
    """
    orgs = spec.orgs if spec.orgs is not None else [org]
    default_org = spec.default_org if spec.default_org is not None else org
    known = {registered.name for registered in registry.list()}
    errors: list[FieldError] = []
    for index, bound in enumerate(orgs):
        if bound not in known:
            errors.append(_field(f"orgs.{index}", f"'{bound}' is not a registered org"))
        elif orgs.index(bound) != index:
            errors.append(_field(f"orgs.{index}", f"'{bound}' is listed twice"))
    if org not in orgs:
        errors.append(_field("orgs", f"the owning org '{org}' is not one of them"))
    if default_org not in orgs:
        errors.append(
            _field(
                "default_org",
                f"'{default_org}' is not one of orgs, so its records would land outside the "
                "binding the receiver holds them to",
            )
        )
    if len(set(spec.listeners)) != len(spec.listeners):
        errors.append(_field("listeners", "a listener is listed twice"))
    try:
        bearer_secret_source(spec.listeners[0], backend)
    except BearerSourceUnreadableError as exc:
        errors.append(_field("listeners", str(exc)))
    if errors:
        raise ProblemError(
            422,
            "The credential's binding is one the receiver could not hold",
            code="invalid_binding",
            errors=errors,
        )
    return IngestCredential(
        name=name, org=org, orgs=orgs, default_org=default_org, listeners=spec.listeners
    )


def _check_bound_orgs(request: Request, user: Any, credential: IngestCredential) -> None:
    """``org:write`` at every org the credential binds beyond its owner, which the route checked."""
    for bound in credential.orgs:
        if bound != credential.org:
            check_action(request, user, scopes_dict["org_write"], scope=Scope(type="org", id=bound))


def _secrets_failure(exc: Exception) -> HTTPException:
    return backend_failure(
        503,
        "secrets_unavailable",
        "the secrets backend refused an ingest credential token; the engine log has the reason",
        exc,
        event="ingest credential token not written",
    )


# -- Writes, run off the event loop ------------------------------------------


def _sync_tokens(request: Request) -> None:
    """Rewrite the receiver's bearer secrets from the credentials as they now are.

    Raises:
        HTTPException: 503 ``secrets_unavailable`` when the secrets backend refuses.
    """
    try:
        sync_bearer_tokens(
            _registry(request),
            _secrets(request),
            getattr(request.app.state, "bearer_mirror", None),
        )
    except SecretsError as exc:
        raise _secrets_failure(exc) from exc


def _changed_since_read(
    request: Request, credential: IngestCredential, preconditions: Preconditions
) -> ProblemError:
    """The answer for a write another write got to first, judged on the credential as it is now.

    A precondition that no longer holds answers its own 412, with the ETag as it is now.
    One that still holds, or none, lost to a change no ETag shows, such as a rotate's
    new token, and answers 409 ``conflict``.
    """
    now = _owned(_registry(request), credential.org, credential.name)
    try:
        preconditions.check(None if now is None else credential_etag(now), require=False)
    except ProblemError as failed:
        return failed
    return ProblemError(
        409,
        "The ingest credential changed while this request was writing it, retry",
        code=ErrorCode.CONFLICT,
    )


def _store(
    request: Request,
    credential: IngestCredential,
    preconditions: Preconditions,
    *,
    new: bool = False,
    current: IngestCredential | None = None,
) -> None:
    """Store *credential*.

    ``new`` stores it only where no credential has its id, and ``current`` only where
    the stored one is still that credential.

    Raises:
        HTTPException: 409 ``credential_id_taken`` when another org took the id first.
        ProblemError: 412 or 409 ``conflict`` when another write created, changed or
            removed it since, as :func:`_changed_since_read` judges.
    """
    try:
        _registry(request).ingest_credentials.put(credential, new=new, current=current)
    except CredentialIdTakenError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "credential_id_taken", "message": str(exc)}
        ) from exc
    except CredentialChangedError as exc:
        raise _changed_since_read(request, credential, preconditions) from exc


def _put(
    request: Request,
    credential: IngestCredential,
    current: IngestCredential,
    preconditions: Preconditions,
) -> None:
    """Store *credential* where the stored one is still *current*, then rewrite the bearer secrets.

    *credential* names *current*'s token, so a rotate that lands first keeps its own: its
    record and new token stand, and this write changes nothing.

    Raises:
        HTTPException: 409 ``credential_id_taken`` when another org took the id first,
            503 ``secrets_unavailable`` when the secrets backend refuses.
        ProblemError: 412 or 409 ``conflict`` when another write changed or removed it since.
    """
    _store(request, credential, preconditions, current=current)
    _sync_tokens(request)


def _drop_quietly(secrets: DfeSecrets, credential: IngestCredential) -> None:
    """Drop the token stored under *credential*'s ``token_id``, logging a backend that refuses."""
    try:
        drop_token(secrets, credential)
    except SecretsError as exc:
        logger.warning(
            "ingest credential token not dropped", ingest_id=credential.name, error=str(exc)
        )


def _drop_unless_named(request: Request, credential: IngestCredential) -> None:
    """Drop the token stored under *credential*'s ``token_id`` unless the stored record names it.

    A put-back that never landed leaves the record on this token, and dropping it would
    leave a record pointing at nothing. A record that cannot be read keeps the token too,
    because an orphan token is inert.
    """
    try:
        stored = _registry(request).ingest_credentials.get(credential.name)
    except Exception as exc:  # The caller's own failure must reach it, whatever the read raises.
        logger.warning(
            "ingest credential token kept, its record could not be read",
            ingest_id=credential.name,
            path=credential_secret_path(credential),
            error=str(exc),
        )
        return
    if stored is None or stored.token_id != credential.token_id:
        _drop_quietly(_secrets(request), credential)


def _put_back_record(
    request: Request, credential: IngestCredential, previous: IngestCredential | None
) -> None:
    """Restore *previous*, or remove the record a create stored, while it is still *credential*.

    Either write fires the org listeners, so the bearer secrets are rewritten from what
    stood before. A record another write removed, changed or took since is left as that
    write made it, and logged. Any other failure raises.
    """
    credentials = _registry(request).ingest_credentials
    try:
        if previous is None:
            credentials.delete(credential.name, current=credential)
        else:
            credentials.put(previous, current=credential)
    except (KeyError, CredentialChangedError, CredentialIdTakenError) as exc:
        logger.warning("ingest credential not put back", ingest_id=credential.name, error=str(exc))


def _mint(
    request: Request,
    credential: IngestCredential,
    token: str,
    preconditions: Preconditions,
    *,
    previous: IngestCredential | None = None,
) -> None:
    """Store *token* under the credential's own ``token_id``, then the credential, then the bearer secrets.

    The token goes first, so a stored credential never names a token that is not
    there. Its secret is this mint's alone, so storing it touches no other write's
    token. The record then decides a race: a create stores it only where no credential
    has its id, a rotate only where the stored one is still *previous*. The write that
    loses drops its own token and nothing else. A replica that dies between the two
    leaves a token no record names, which nothing ever presents.

    A write that cannot finish puts back what it changed: its caller got no token, so
    the API, the store and the receiver agree only on what stood before. A create
    leaves nothing of the credential. A rotate leaves the old record, whose token it
    never touched. The token this write minted is dropped once the put-back has ended,
    unless the stored record still names it, as it does after a put-back that never
    landed. A put-back that fails raises in place of the caller's error. A rotate that
    finishes drops the old token.

    Raises:
        HTTPException: 409 ``credential_id_taken`` when another org took the id first,
            503 ``secrets_unavailable`` when the secrets backend or mirror refuses.
        ProblemError: 412 or 409 ``conflict`` when another write created, changed or
            removed it first.
    """
    secrets = _secrets(request)
    try:
        store_token(secrets, credential, token)
    except SecretsError as exc:
        raise _secrets_failure(exc) from exc
    try:
        _store(request, credential, preconditions, new=previous is None, current=previous)
    except BaseException:
        _drop_quietly(secrets, credential)
        raise
    try:
        _sync_tokens(request)
    except HTTPException:
        try:
            _put_back_record(request, credential, previous)
        finally:
            _drop_unless_named(request, credential)
        raise
    if previous is not None:
        _drop_quietly(secrets, previous)


def _delete(request: Request, credential: IngestCredential, preconditions: Preconditions) -> None:
    """Remove the credential where it is still *credential*, then rewrite the bearer secrets.

    A rotate that lands first keeps the credential and its new token, and this delete
    changes nothing. The org registry's removal listener drops the token, as it does
    for an org's cascade.

    Raises:
        HTTPException: 503 ``secrets_unavailable`` when the secrets backend refuses.
        ProblemError: 412 or 409 ``conflict`` when another write changed or removed it since.
    """
    try:
        _registry(request).ingest_credentials.delete(credential.name, current=credential)
    except (KeyError, CredentialChangedError) as exc:
        raise _changed_since_read(request, credential, preconditions) from exc
    _sync_tokens(request)


# -- Endpoints ---------------------------------------------------------------


@router.get("", response_model=Page[IngestCredentialResponse], dependencies=[_READ])
def list_ingest_credentials(
    org: str, request: Request, query: ListParams
) -> Page[IngestCredentialResponse]:
    """List the ingest credentials an org owns, by id. Takes no filter or order_by."""
    if query.filter or query.order_by:
        raise ProblemError(
            400, "This list is ordered by id and takes no filter", code="unsupported_query"
        )
    registry = _registry(request)
    _require_org(registry, org)
    return paginate([_response(c) for c in registry.ingest_credentials.owned_by(org)], query)


@router.get("/{credential}", response_model=IngestCredentialResponse, dependencies=[_READ])
def get_ingest_credential(
    org: str, credential: str, request: Request, response: Response
) -> IngestCredentialResponse:
    """Get one ingest credential, never its token. Its ``ETag`` is an apply's ``If-Match``."""
    held = _require_owned(_registry(request), org, credential)
    response.headers["ETag"] = credential_etag(held)
    return _response(held)


@router.put("/{credential}", response_model=IngestCredentialWriteResponse, dependencies=[_WRITE])
async def apply_ingest_credential(
    org: str,
    credential: str,
    body: IngestCredentialSpec,
    user: CurrentUser,
    request: Request,
    response: Response,
    preconditions: PreconditionHeaders,
    dry_run: DryRun,
) -> IngestCredentialWriteResponse:
    """Create an ingest credential and mint its token, or change what it binds.

    ``If-None-Match: *`` creates it and answers 201 with the token, once.
    ``If-Match: <etag>`` updates its orgs, default org and listeners and keeps its
    token. A credential id is unique across every org, so creating one another org
    holds answers 409 ``credential_id_taken``. A binding the receiver could not
    hold answers 422 ``invalid_binding`` with a field error for each problem. A write
    another one got to first answers 412 where its precondition no longer holds, else
    409 ``conflict``, and changes nothing.
    """
    if re.fullmatch(CREDENTIAL_ID_PATTERN, credential) is None:
        raise ProblemError(
            422,
            "A credential id is 1 to 128 letters, digits, '.', '_' or '-', starting with a "
            "letter or digit",
            code="invalid_credential_id",
        )
    registry = _registry(request)
    _require_org(registry, org)
    current = _owned(registry, org, credential)
    preconditions.check(None if current is None else credential_etag(current))
    if current is None and registry.ingest_credentials.get(credential) is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "credential_id_taken",
                "message": f"Ingest credential id '{credential}' is taken: ids are unique "
                "across every org because the receiver keys its bindings on them",
            },
        )
    desired = _desired(registry, org, credential, body, get_settings().secrets)
    _check_bound_orgs(request, user, desired)
    response.headers["ETag"] = credential_etag(desired)

    if current is not None:
        desired = desired.model_copy(
            update={
                "created_at": current.created_at,
                "rotated_at": current.rotated_at,
                "token_id": current.token_id,
            }
        )
        if dry_run:
            return _written(desired, dry_run=True)
        await run_blocking(functools.partial(_put, request, desired, current, preconditions))
        audit_resource_change(user.user_id, _RESOURCE, credential, "updated", {"org": org})
        synced = await run_blocking(functools.partial(reconcile_tenancy, request, user))
        return _written(desired, dry_run=False, synced=synced)

    if dry_run:
        return _written(desired, dry_run=True)
    now = _now()
    minted = desired.model_copy(
        update={"created_at": now, "rotated_at": now, "token_id": new_token_id()}
    )
    token = mint_token()
    await run_blocking(functools.partial(_mint, request, minted, token, preconditions))
    audit_resource_change(user.user_id, _RESOURCE, credential, "created", {"org": org})
    synced = await run_blocking(functools.partial(reconcile_tenancy, request, user))
    response.status_code = 201
    return _written(minted, dry_run=False, token=token, synced=synced)


@router.delete("/{credential}", status_code=204, dependencies=[_WRITE])
async def delete_ingest_credential(
    org: str,
    credential: str,
    user: CurrentUser,
    request: Request,
    preconditions: PreconditionHeaders,
    dry_run: DryRun,
) -> None:
    """Delete an ingest credential. Its token stops working once the receiver rereads its secret.

    One another write got to first answers 412 where its ``If-Match`` no longer holds,
    else 409 ``conflict``, and changes nothing.
    """
    held = _require_owned(_registry(request), org, credential)
    preconditions.check(credential_etag(held))
    if dry_run:
        return
    await run_blocking(functools.partial(_delete, request, held, preconditions))
    audit_resource_change(user.user_id, _RESOURCE, credential, "deleted", {"org": org})
    await run_blocking(functools.partial(reconcile_tenancy, request, user))


@router.post(
    "/{credential}/rotate", response_model=IngestCredentialWriteResponse, dependencies=[_WRITE]
)
async def rotate_ingest_credential(
    org: str,
    credential: str,
    user: CurrentUser,
    request: Request,
    response: Response,
    preconditions: PreconditionHeaders,
    dry_run: DryRun,
) -> IngestCredentialWriteResponse:
    """Mint a new token for an ingest credential. The old one stops working and the binding stays.

    The new token comes back in this response and never again. ``If-Match`` is
    honoured when sent. A rotate the secrets backend refuses answers 503 and leaves the
    old token working. One another write got to first answers 412 where its
    ``If-Match`` no longer holds, else 409 ``conflict``, and changes nothing.
    """
    held = _require_owned(_registry(request), org, credential)
    preconditions.check(credential_etag(held), require=False)
    response.headers["ETag"] = credential_etag(held)
    if dry_run:
        return _written(held, dry_run=True)
    rotated = held.model_copy(update={"rotated_at": _now(), "token_id": new_token_id()})
    token = mint_token()
    await run_blocking(
        functools.partial(_mint, request, rotated, token, preconditions, previous=held)
    )
    audit_resource_change(user.user_id, _RESOURCE, credential, "rotated", {"org": org})
    return _written(rotated, dry_run=False, token=token)
