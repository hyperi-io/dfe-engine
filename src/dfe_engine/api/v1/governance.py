#  Project:      dfe-engine
#  File:         api/v1/governance.py
#  Purpose:      Governed Ops Tier-2 - defined actions + admin CRUD
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Tier-2 defined actions (the curated big dials) + admin CRUD of actions/policies.

GET  /api/v1/governance/actions                 -> list actions (governance:read)
GET  /api/v1/governance/actions/{name}          -> get an action (governance:read)
POST /api/v1/governance/actions/{name}/invoke   -> invoke (per-action required_action)
GET  /api/v1/governance/policies                -> list policies (governance:read)
GET  /api/v1/governance/policies/{name}         -> get a policy (governance:read)
POST /api/v1/governance/admin/actions/validate  -> check a def + diff, no commit
POST/PUT/DELETE /api/v1/governance/admin/actions[/{name}]   -> CRUD defs (governance:write)
POST/DELETE     /api/v1/governance/admin/policies[/{name}]  -> CRUD policies (governance:write)

Operators get curated actions via `action:invoke:<name>`; raw class CRUD stays with
admins (governance:write). Everything commits to gitops via the engine.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from scalo.logger import logger

from dfe_engine.api.deps import CurrentUser, live_role_config, require_action
from dfe_engine.api.review import apply_review_headers
from dfe_engine.api.write_turn import WRITE_TURN
from dfe_engine.appmgmt import contract
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.engine import authorize
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.gitcrud import GitCrud, ResourceNotFoundError
from dfe_engine.gitcrud.commit_policy import CommitPolicyError, validate_name
from dfe_engine.gitcrud.routing import ReviewRequiredError, WriteOutcome, route_write
from dfe_engine.governance import (
    ActionDef,
    ActionForbiddenError,
    ActionStore,
    CredentialInActionError,
    InvalidDocumentError,
    InvalidParamsError,
    PolicyStore,
    ProtectedPolicy,
    ProtectedVarError,
)

router = APIRouter(prefix="/governance", tags=["Governed Ops: Actions"], dependencies=[WRITE_TURN])

_POLICY_CLASS = "policies"

_CH_UNAVAILABLE = "ClickHouse is unreachable; the engine log has the connection error"


class InvokeRequest(BaseModel):
    """Optional invoke body: values for the action's declared params."""

    params: dict[str, Any] = {}


class InvokeResponse(BaseModel):
    dry_run: bool
    changed: bool
    commit_sha: str | None = None
    auto_merged: bool = False
    # Set when a production+team invoke was routed to a review PR instead of main.
    review_required: bool = False
    pr_url: str | None = None
    diff: list[dict[str, Any]]


def _gitcrud(request: Request) -> GitCrud:
    gc = getattr(request.app.state, "gitcrud", None)
    if gc is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "not_configured", "message": "gitops is not enabled"},
        )
    return gc


def _actions(request: Request) -> ActionStore:
    return ActionStore(_gitcrud(request))


def _policies(request: Request) -> PolicyStore:
    return PolicyStore(_gitcrud(request))


def _forge(request: Request):
    """The deploy-repo forge client for opening review PRs (None -> refuse)."""
    return getattr(request.app.state, "forge", None)


def _check_name(name: str) -> None:
    """400 on a resource name that could traverse the tree or forge a trailer."""
    try:
        validate_name(name)
    except CommitPolicyError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "invalid_name", "message": str(exc)}
        ) from exc


def _route_admin_write(
    request: Request, *, resource: str, actor: str, title: str, body: str, write
) -> WriteOutcome:
    """route_write for a governance-class admin CRUD write (201/204 endpoints).

    Direct commit in dev/solo; a production+team write is pushed to a review branch
    and a PR opened (or 409 ``review_required`` when no forge is configured). The
    caller surfaces any PR via response headers since these bodies are fixed.
    """
    settings = request.app.state.settings
    try:
        return route_write(
            gc=_gitcrud(request),
            forge=_forge(request),
            environment=settings.env,
            mode=settings.gitops.mode,
            rbac_class="governance",
            resource=resource,
            actor=actor,
            title=title,
            body=body,
            write=write,
        )
    except ReviewRequiredError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "review_required", "message": str(exc)}
        ) from exc


@router.get("/actions", dependencies=[Depends(require_action(scopes_dict["governance_read"]))])
def list_actions(user: CurrentUser, request: Request) -> list[str]:
    return _actions(request).list()


@router.get(
    "/actions/{name}", dependencies=[Depends(require_action(scopes_dict["governance_read"]))]
)
def get_action(name: str, user: CurrentUser, request: Request) -> ActionDef:
    """One defined action; a credential a legacy definition still carries comes back masked."""
    try:
        return _actions(request).get_shown(name)
    except ResourceNotFoundError as exc:
        raise HTTPException(404, detail={"code": "not_found", "message": str(exc)}) from exc


@router.get("/policies", dependencies=[Depends(require_action(scopes_dict["governance_read"]))])
def list_policies(user: CurrentUser, request: Request) -> list[str]:
    return _policies(request).list()


@router.get(
    "/policies/{name}", dependencies=[Depends(require_action(scopes_dict["governance_read"]))]
)
def get_policy(name: str, user: CurrentUser, request: Request) -> ProtectedPolicy:
    _check_name(name)
    try:
        return _policies(request).get(name)
    except ResourceNotFoundError as exc:
        raise HTTPException(404, detail={"code": "not_found", "message": str(exc)}) from exc


@router.post("/actions/{name}/invoke", response_model=InvokeResponse)
def invoke_action(
    name: str,
    user: CurrentUser,
    request: Request,
    body: InvokeRequest | None = None,
    dry_run: bool = Query(default=False),
) -> InvokeResponse:
    """Invoke a defined action - gated on the action's OWN required_action.

    ``body.params`` supplies values for the action's declared params (422 on a
    constraint violation; omitted params take their defaults). A hunt or rule
    document the action would leave invalid is 422 ``invalid_document``, with the
    hunts API's own message. Direct commit in dev/solo; a production+team invoke is
    routed to a review PR (or 409 ``review_required`` when no forge is configured).
    """
    _check_name(name)
    store = _actions(request)
    settings = request.app.state.settings
    params = body.params if body else None
    try:
        action = store.get(name)
    except ResourceNotFoundError as exc:
        raise HTTPException(404, detail={"code": "not_found", "message": str(exc)}) from exc

    # Per-action RBAC: the action declares the permission needed to run it.
    decision = authorize(user, action.required_action, role_config=request.app.state.role_config)
    if not decision.allowed:
        raise HTTPException(403, detail={"code": "forbidden", "message": decision.reason})

    policy = getattr(request.app.state, "policy_store", None)
    override = authorize(
        user, "helmvars:override", role_config=request.app.state.role_config
    ).allowed

    # Validate + build the diff via a dry run first: this surfaces protected-var /
    # forbidden / policy / param violations BEFORE any review-vs-direct routing,
    # so a bad invoke never opens a PR (and 4xx keeps precedence over 409).
    try:
        preview = store.invoke(
            name, user.user_id, params=params, policy=policy, dry_run=True, override=override
        )
    except InvalidParamsError as exc:
        raise HTTPException(422, detail={"code": "invalid_params", "message": str(exc)}) from exc
    except InvalidDocumentError as exc:
        raise HTTPException(422, detail={"code": "invalid_document", "message": str(exc)}) from exc
    except ProtectedVarError as exc:
        raise HTTPException(403, detail={"code": "protected_var", "message": str(exc)}) from exc
    except ActionForbiddenError as exc:
        raise HTTPException(403, detail={"code": "action_forbidden", "message": str(exc)}) from exc
    except CommitPolicyError as exc:
        raise HTTPException(403, detail={"code": "policy_violation", "message": str(exc)}) from exc
    except contract.MaskedValueError as exc:
        raise HTTPException(400, detail={"code": exc.code, "message": str(exc)}) from exc

    if dry_run:
        return InvokeResponse(dry_run=True, changed=False, commit_sha=None, diff=preview.diff)

    def _write(branch: str):
        return store.invoke(
            name,
            user.user_id,
            params=params,
            policy=policy,
            dry_run=False,
            override=override,
            branch=branch,
        )

    try:
        outcome = route_write(
            gc=_gitcrud(request),
            forge=_forge(request),
            environment=settings.env,
            mode=settings.gitops.mode,
            rbac_class="governance",
            resource=f"action/{name}",
            actor=user.user_id,
            title=f"action({name}): invoke",
            body=f"Invoke action '{name}' by {user.user_id}. Opened for review "
            "because production+team may not commit straight to main.",
            write=_write,
        )
    except ReviewRequiredError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "review_required", "message": str(exc)}
        ) from exc
    audit_resource_change(
        user.user_id,
        "action",
        name,
        "invoked",
        {"commit": outcome.commit_sha, "pr": outcome.pr_url, "params": params or {}},
    )
    return InvokeResponse(
        dry_run=False,
        changed=outcome.changed,
        commit_sha=outcome.commit_sha,
        auto_merged=outcome.auto_merged,
        review_required=outcome.review_required,
        pr_url=outcome.pr_url,
        diff=preview.diff,
    )


class ValidateResponse(BaseModel):
    """Outcome of checking an action definition without saving or invoking it."""

    valid: bool
    errors: list[str]
    diff: list[dict[str, Any]]


@router.post(
    "/admin/actions/validate",
    response_model=ValidateResponse,
    dependencies=[Depends(require_action("governance:write"))],
)
def validate_action(body: ActionDef, user: CurrentUser, request: Request) -> ValidateResponse:
    """Dry-check an action definition: every violation + the would-be diff.

    Nothing is committed - this is the authoring hand-hold, run before the
    define endpoint so a wizard can show the full diff and every problem in
    one round trip.
    """
    _check_name(body.name)
    policy = getattr(request.app.state, "policy_store", None)
    override = authorize(
        user, "helmvars:override", role_config=request.app.state.role_config
    ).allowed
    diff, errors = _actions(request).preview(body, policy=policy, override=override)
    return ValidateResponse(valid=not errors, errors=errors, diff=diff)


@router.post(
    "/admin/actions",
    status_code=201,
    dependencies=[Depends(require_action(scopes_dict["governance_write"]))],
)
def create_action(
    body: ActionDef, user: CurrentUser, request: Request, response: Response
) -> ActionDef:
    """Define or replace an action.

    400 ``credential_in_action`` where a change targets a credential: the definition
    is committed to the deploy repo, so a credential in it is plaintext in history.
    """
    _check_name(body.name)
    try:
        _actions(request).refuse_credentials(body)
    except CredentialInActionError as exc:
        raise HTTPException(
            400, detail={"code": "credential_in_action", "message": str(exc)}
        ) from exc

    def _write(branch: str):
        return _actions(request).save(body, user.user_id, branch=branch)

    outcome = _route_admin_write(
        request,
        resource=f"action/{body.name}",
        actor=user.user_id,
        title=f"action({body.name}): define",
        body=f"Define action '{body.name}' by {user.user_id}. Opened for review "
        "because production+team may not commit straight to main.",
        write=_write,
    )
    audit_resource_change(user.user_id, "action", body.name, "created")
    apply_review_headers(response, outcome)
    return body


@router.delete(
    "/admin/actions/{name}",
    status_code=204,
    dependencies=[Depends(require_action(scopes_dict["governance_write"]))],
)
def delete_action(name: str, user: CurrentUser, request: Request, response: Response) -> None:
    _check_name(name)
    # Surface 404 before any review routing.
    try:
        _actions(request).get(name)
    except ResourceNotFoundError as exc:
        raise HTTPException(404, detail={"code": "not_found", "message": str(exc)}) from exc

    def _write(branch: str):
        return _actions(request).delete(name, user.user_id, branch=branch)

    outcome = _route_admin_write(
        request,
        resource=f"action/{name}",
        actor=user.user_id,
        title=f"action({name}): delete",
        body=f"Delete action '{name}' by {user.user_id}. Opened for review "
        "because production+team may not commit straight to main.",
        write=_write,
    )
    audit_resource_change(user.user_id, "action", name, "deleted")
    apply_review_headers(response, outcome)


@router.post(
    "/admin/policies",
    status_code=201,
    dependencies=[Depends(require_action(scopes_dict["governance_write"]))],
)
def create_policy(
    body: ProtectedPolicy, user: CurrentUser, request: Request, response: Response
) -> ProtectedPolicy:
    _check_name(body.name)
    gc = _gitcrud(request)

    def _write(branch: str):
        return gc.put(_POLICY_CLASS, body.name, body.model_dump(), user.user_id, branch=branch)

    outcome = _route_admin_write(
        request,
        resource=f"policy/{body.name}",
        actor=user.user_id,
        title=f"rbac({body.name}): define protected-var policy",
        body=f"Define protected-var policy '{body.name}' by {user.user_id}. Opened "
        "for review because production+team may not commit straight to main.",
        write=_write,
    )
    audit_resource_change(user.user_id, "policy", body.name, "created")
    apply_review_headers(response, outcome)
    return body


@router.delete(
    "/admin/policies/{name}",
    status_code=204,
    dependencies=[Depends(require_action(scopes_dict["governance_write"]))],
)
def delete_policy(name: str, user: CurrentUser, request: Request, response: Response) -> None:
    _check_name(name)
    gc = _gitcrud(request)
    # Surface 404 before any review routing.
    try:
        gc.get(_POLICY_CLASS, name)
    except ResourceNotFoundError as exc:
        raise HTTPException(404, detail={"code": "not_found", "message": str(exc)}) from exc

    def _write(branch: str):
        return gc.delete(_POLICY_CLASS, name, user.user_id, branch=branch)

    outcome = _route_admin_write(
        request,
        resource=f"policy/{name}",
        actor=user.user_id,
        title=f"rbac({name}): delete protected-var policy",
        body=f"Delete protected-var policy '{name}' by {user.user_id}. Opened for "
        "review because production+team may not commit straight to main.",
        write=_write,
    )
    audit_resource_change(user.user_id, "policy", name, "deleted")
    apply_review_headers(response, outcome)


@router.post(
    "/ch-rbac/reconcile",
    dependencies=[Depends(require_action(scopes_dict["governance_write"]))],
)
def reconcile_ch_rbac_endpoint(user: CurrentUser, request: Request) -> dict[str, Any]:
    """Reconcile CH quota tiers + service roles + per-org row policies into
    ClickHouse, minting the service-user secrets via the secrets seam. Idempotent.
    governance:write.
    """
    from dfe_engine.clickhouse.errors import is_connection_error
    from dfe_engine.governance.ch import (
        ch_admin_client,
        note_ch_rbac_reconciled,
        reconcile_from_stores,
    )
    from dfe_engine.settings import load_settings

    settings = load_settings()
    try:
        admin_client = ch_admin_client(settings)
    except Exception as exc:
        logger.warning("CH RBAC reconcile: ClickHouse unreachable", error=str(exc))
        raise HTTPException(
            status_code=503,
            detail={"code": "clickhouse_unavailable", "message": _CH_UNAVAILABLE},
        ) from exc

    try:
        result = reconcile_from_stores(
            admin_client,
            settings=settings,
            org_registry=getattr(request.app.state, "org_registry", None),
            group_store=getattr(request.app.state, "group_store", None),
            role_config=live_role_config(request),
        )
    except Exception as exc:
        if not is_connection_error(exc):
            raise
        logger.warning("CH RBAC reconcile: ClickHouse connection lost", error=str(exc))
        raise HTTPException(
            status_code=503,
            detail={"code": "clickhouse_unavailable", "message": _CH_UNAVAILABLE},
        ) from exc
    # A full run covers every identity, so a startup retry still waiting stands down.
    note_ch_rbac_reconciled(request.app.state)
    return {
        "statements": len(result.statements),
        "dropped": len(result.dropped),
        "minted": result.minted,
        "errors": result.errors,
    }
