#  Project:      dfe-engine
#  File:         api/v1/library.py
#  Purpose:      CRUD over the versioned artefact library
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The versioned library of authored files, generic over what a file IS.

GET    /api/v1/library                             list artefacts, filtered
POST   /api/v1/library                             create one, optionally empty
GET    /api/v1/library/kinds                       the kinds the manifest declares
GET    /api/v1/library/{artifact}                  metadata and pointers
PATCH  /api/v1/library/{artifact}                  edit description, labels, group
DELETE /api/v1/library/{artifact}                  refused while linked
GET    /api/v1/library/{artifact}/versions         version history
POST   /api/v1/library/{artifact}/versions         publish a version
GET    /api/v1/library/{artifact}/versions/{v}     one immutable version
POST   /api/v1/library/{artifact}/rollback         repoint current at an earlier version
PUT    /api/v1/library/{artifact}/state            enabled | disabled | deprecated
PUT    /api/v1/library/{artifact}/tags/{tag}       point a tag at a version
DELETE /api/v1/library/{artifact}/tags/{tag}       remove a tag
GET    /api/v1/library/{artifact}/usage            which instances link to it

Every mutation is a git commit through the same gitcrud path ``api/v1/helm.py``
uses, so the protected-var policy, review routing and audit apply unchanged.

Content is immutable once published: a version read carries a strong ETag equal to
the version's digest, and publishing different content over an existing version is
refused. Metadata and tags are mutable and never produce a version.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from dfe_engine.api.deps import CurrentUser, require_action
from dfe_engine.appmgmt import library, links, validate_language
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.engine import authorize
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.gitcrud import ConcurrencyConflictError, GitCrud
from dfe_engine.gitcrud.commit_policy import (
    SUBJECT_MAX,
    CommitContext,
    CommitPolicyError,
    build_message,
    validate_name,
)
from dfe_engine.gitcrud.engine import ResourceNotFoundError, flatten
from dfe_engine.gitcrud.routing import ReviewRequiredError, route_write
from dfe_engine.gitcrud.versioned import VersionConflictError
from dfe_engine.governance import PolicyStore, ProtectedVarError

router = APIRouter(prefix="/library", tags=["Artefact Library"])

_CLASS = links.LIBRARY_CLASS

_READ = Depends(require_action(scopes_dict["library_read"]))
_WRITE = Depends(require_action(scopes_dict["library_write"]))


# ── request and response models ───────────────────────────────


class KindModel(BaseModel):
    name: str
    language: str
    suffixes: list[str]
    encoding: str


class ValidationModel(BaseModel):
    status: str = Field(
        description=(
            "'valid' or 'invalid' when a backend answered, 'unavailable' when none "
            "could, 'disabled' when validation is off for this deployment."
        )
    )
    backend: str = ""
    message: str = ""
    errors: list[str] = Field(default_factory=list)


class WriteResult(BaseModel):
    changed: bool
    commit_sha: str | None = None
    auto_merged: bool = False
    review_required: bool = False
    pr_url: str | None = None
    version: int | None = Field(
        default=None, description="The version this write published or repointed to."
    )
    validation: ValidationModel | None = None


class ArtifactModel(BaseModel):
    name: str
    kind: str
    state: str
    group: str = ""
    description: str = ""
    labels: dict[str, str] = Field(default_factory=dict)
    current: int | None = None
    versions: list[int] = Field(default_factory=list)
    tags: dict[str, int] = Field(
        default_factory=dict, description="Mutable names pointing at a version."
    )
    digest: str = Field(default="", description="Digest of the current version.")


class VersionSummary(BaseModel):
    version: int
    digest: str
    size_bytes: int
    description: str = ""
    published_by: str = ""
    published_at: int = 0
    message: str = ""


class VersionDetail(VersionSummary):
    kind: str
    encoding: str
    content: str


class CreateArtifactRequest(BaseModel):
    name: str
    kind: str = Field(description="A kind declared in the app manifest.")
    group: str = Field(default="", description="Optional namespace, e.g. 'network'.")
    description: str = ""
    labels: dict[str, str] = Field(default_factory=dict)
    content: str | None = Field(
        default=None,
        description="Publish this as version 1. Omit to create the artefact empty.",
    )
    message: str = ""


class PublishVersionRequest(BaseModel):
    content: str
    description: str = Field(default="", description="Fixed to this version at publish.")
    message: str = ""
    version: int | None = Field(
        default=None,
        description="Publish at this exact version. Omit for the next in the series.",
    )


class PatchArtifactRequest(BaseModel):
    description: str | None = None
    labels: dict[str, str] | None = None
    group: str | None = None


class StateRequest(BaseModel):
    state: str = Field(description="enabled | disabled | deprecated")


class RollbackRequest(BaseModel):
    version: int


class TagRequest(BaseModel):
    version: int


class UsageModel(BaseModel):
    service: str
    instance: str
    file_set: str
    name: str
    version: int
    tag: str = ""


# ── shared plumbing ───────────────────────────────────────────


def _gitcrud(request: Request) -> GitCrud:
    gc = getattr(request.app.state, "gitcrud", None)
    if gc is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "not_configured", "message": "gitops is not enabled"},
        )
    return gc


def _policy(request: Request) -> PolicyStore | None:
    return getattr(request.app.state, "policy_store", None)


def _forge(request: Request):
    return getattr(request.app.state, "forge", None)


def _check_name(name: str) -> str:
    """400 on a name that could traverse the tree or forge a commit trailer."""
    try:
        validate_name(name)
    except CommitPolicyError as exc:
        raise HTTPException(400, detail={"code": "invalid_name", "message": str(exc)}) from exc
    return name


def _kind(name: str):
    try:
        return library.kind(name)
    except library.UnknownKindError:
        declared = ", ".join(k.name for k in library.kinds()) or "none"
        raise HTTPException(
            400,
            detail={
                "code": "unknown_kind",
                "message": f"no kind {name!r} is declared (declared: {declared})",
            },
        ) from None


def _state(value: str) -> library.LifecycleState:
    try:
        return library.LifecycleState(value)
    except ValueError:
        allowed = ", ".join(str(s) for s in library.LifecycleState)
        raise HTTPException(
            400,
            detail={"code": "invalid_state", "message": f"expected one of: {allowed}"},
        ) from None


def _envelope(gc: GitCrud, name: str) -> dict:
    try:
        return gc.get(_CLASS, name)
    except ResourceNotFoundError:
        raise HTTPException(
            404, detail={"code": "no_such_artifact", "message": f"no artefact {name!r}"}
        ) from None


def _version(env: dict, version: int) -> library.ArtifactVersion:
    try:
        return library.read_version(env, version)
    except library.VersionNotFoundError:
        raise HTTPException(
            404, detail={"code": "no_such_version", "message": f"no version {version}"}
        ) from None


def _content(gc: GitCrud, name: str, env: dict, found: library.ArtifactVersion) -> str:
    """A version's content in the text form the API returns it in."""
    try:
        raw = gc.read_payload_bytes(_CLASS, name, found.path)
    except ResourceNotFoundError:
        raise HTTPException(
            409,
            detail={
                "code": "missing_content",
                "message": f"version {found.version} names {found.path!r}, "
                "which is not in the repo",
            },
        ) from None
    return library.as_text(library.artifact_kind_of(env), raw)


def _summary_model(summary: library.ArtifactSummary) -> ArtifactModel:
    return ArtifactModel(
        name=summary.name,
        kind=summary.kind,
        state=str(summary.state),
        group=summary.group,
        description=summary.description,
        labels=dict(summary.labels),
        current=summary.current,
        versions=list(summary.versions),
        tags=dict(summary.tags),
        digest=summary.digest,
    )


def _version_summary(found: library.ArtifactVersion) -> VersionSummary:
    return VersionSummary(
        version=found.version,
        digest=found.digest,
        size_bytes=found.size_bytes,
        description=found.description,
        published_by=found.published_by,
        published_at=found.published_at,
        message=found.message,
    )


def _validate(request: Request, language: str, content: str) -> ValidationModel:
    """Check authored content against this deployment's validation posture."""
    result = validate_language(
        language, content, enabled=request.app.state.settings.transform_validation.enabled
    )
    if result.blocks_write and request.app.state.settings.transform_validation.blocking:
        raise HTTPException(
            400,
            detail={
                "code": "invalid_content",
                "message": result.message,
                "errors": list(result.errors),
                "backend": result.backend,
            },
        )
    return ValidationModel(
        status=str(result.status),
        backend=result.backend,
        message=result.message,
        errors=list(result.errors),
    )


def _enforce(request: Request, user: Any, name: str, doc: dict) -> bool:
    """Gate every leaf of the finished document, and report whether any is protected.

    The per-path helm-var checks are deliberately not applied here: they police
    image pinning and controller-owned replica counts, which are properties of a
    deployment overlay and have no meaning in an artefact document.
    """
    policy = _policy(request)
    if policy is None:
        return False
    override = authorize(
        user, f"{_CLASS}:override", role_config=request.app.state.role_config
    ).allowed
    protected = False
    for path in flatten(doc):
        if policy.is_protected(_CLASS, name, path):
            protected = True
        try:
            policy.enforce(_CLASS, name, path, override=override)
        except ProtectedVarError as exc:
            raise HTTPException(403, detail={"code": "protected_var", "message": str(exc)}) from exc
    return protected


def _fit_subject(name: str, summary: str) -> tuple[str, str]:
    """Trim the scope and summary so the rendered commit subject fits the policy cap."""
    budget = SUBJECT_MAX - len("cfg(): ")
    summary = summary[:budget]
    if len(name) + len(summary) <= budget:
        return name, summary
    scope = name[: max(1, budget - len(summary))]
    return scope, summary[: max(1, budget - len(scope))]


def _commit(
    request: Request,
    user: Any,
    name: str,
    doc: dict,
    *,
    summary: str,
    protected: bool = False,
    if_match: str | None = None,
    plan: library.BundlePlan | None = None,
) -> WriteResult:
    """Write the artefact through the governed routing path.

    The manifest and any content files the change implies go in ONE commit, so a
    manifest never names a version whose file has not landed yet.
    """
    gc = _gitcrud(request)
    settings = request.app.state.settings
    scope, subject_summary = _fit_subject(name, summary)
    message = build_message(
        CommitContext(
            ctype="cfg",
            scope=scope,
            summary=subject_summary,
            actor=user.user_id,
            role=f"{_CLASS}:write",
            base_revision=if_match or "",
        )
    )

    def _write(branch: str):
        return gc.put_bundle(
            _CLASS,
            name,
            doc,
            user.user_id,
            writes=dict((plan or library.BundlePlan()).writes),
            removals=list((plan or library.BundlePlan()).removals),
            message=message,
            base_revision=if_match,
            branch=branch,
        )

    try:
        outcome = route_write(
            gc=gc,
            forge=_forge(request),
            environment=settings.env,
            mode=settings.gitops.mode,
            rbac_class=_CLASS,
            resource=f"{_CLASS}/{name}",
            actor=user.user_id,
            protected=protected,
            title=f"cfg({scope}): {summary}",
            body=(
                f"Governed change to library artefact {name} by {user.user_id}. "
                "Opened for review because production+team may not commit straight to main."
            ),
            write=_write,
        )
    except ConcurrencyConflictError as exc:
        raise HTTPException(
            409,
            detail={
                "code": "conflict",
                "message": str(exc),
                "current": exc.current,
                "head": exc.head,
            },
        ) from exc
    except ReviewRequiredError as exc:
        raise HTTPException(409, detail={"code": "review_required", "message": str(exc)}) from exc

    audit_resource_change(user.user_id, _CLASS, name, "updated", {"summary": summary})
    return WriteResult(
        changed=outcome.changed,
        commit_sha=outcome.commit_sha,
        auto_merged=outcome.auto_merged,
        review_required=outcome.review_required,
        pr_url=outcome.pr_url,
    )


# ── kinds ─────────────────────────────────────────────────────


@router.get("/kinds", dependencies=[_READ])
async def list_kinds(user: CurrentUser, request: Request) -> list[KindModel]:
    """Every artefact kind the manifest declares."""
    return [
        KindModel(
            name=k.name,
            language=k.language,
            suffixes=list(k.suffixes),
            encoding=str(k.encoding),
        )
        for k in library.kinds()
    ]


# ── artefacts ─────────────────────────────────────────────────


@router.get("", dependencies=[_READ])
async def list_artifacts(
    user: CurrentUser,
    request: Request,
    kind: str = "",
    group: str = "",
    state: str = "",
    label: list[str] = Query(
        default_factory=list,
        description="Repeatable 'key' or 'key=value' selector; all must match.",
    ),
    q: str = Query(default="", description="Substring of the name or the description."),
) -> list[ArtifactModel]:
    """Every artefact, filtered. Different filters AND together."""
    gc = _gitcrud(request)
    out: list[ArtifactModel] = []
    for name in gc.list(_CLASS):
        try:
            summary = library.summarise(name, gc.get(_CLASS, name))
        except (library.InvalidArtifactError, ValueError):
            continue
        if library.matches(summary, kind_name=kind, group=group, state=state, labels=label, text=q):
            out.append(_summary_model(summary))
    return out


@router.post("", response_model=WriteResult, dependencies=[_WRITE])
async def create_artifact(
    body: CreateArtifactRequest, user: CurrentUser, request: Request
) -> WriteResult:
    """Create an artefact, with or without its first version.

    Creating one empty is what lets its kind, group and labels be settled before
    any content can enter.
    """
    name = _check_name(body.name)
    gc = _gitcrud(request)
    try:
        gc.get(_CLASS, name)
    except ResourceNotFoundError:
        pass
    else:
        raise HTTPException(
            409, detail={"code": "already_exists", "message": f"artefact {name!r} already exists"}
        )

    artifact_kind = _kind(body.kind)
    try:
        doc = library.new_artifact(
            artifact_kind,
            group=body.group,
            description=body.description,
            labels=body.labels,
        )
    except library.InvalidArtifactError as exc:
        raise HTTPException(400, detail={"code": "invalid_metadata", "message": str(exc)}) from exc

    checked: ValidationModel | None = None
    version: int | None = None
    plan: library.BundlePlan | None = None
    if body.content is not None:
        checked = _validate(request, artifact_kind.language, body.content)
        try:
            version, _, plan = library.publish(
                doc, content=body.content, actor=user.user_id, message=body.message
            )
        except library.InvalidArtifactError as exc:
            raise HTTPException(
                400, detail={"code": "invalid_content", "message": str(exc)}
            ) from exc

    protected = _enforce(request, user, name, doc)
    result = _commit(
        request, user, name, doc, summary="create artefact", protected=protected, plan=plan
    )
    return result.model_copy(update={"version": version, "validation": checked})


@router.get("/{artifact}", dependencies=[_READ])
async def get_artifact(artifact: str, user: CurrentUser, request: Request) -> ArtifactModel:
    """One artefact's metadata and pointers."""
    name = _check_name(artifact)
    return _summary_model(library.summarise(name, _envelope(_gitcrud(request), name)))


@router.patch("/{artifact}", response_model=WriteResult, dependencies=[_WRITE])
async def patch_artifact(
    artifact: str,
    body: PatchArtifactRequest,
    user: CurrentUser,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> WriteResult:
    """Edit the classification metadata. Never produces a version."""
    name = _check_name(artifact)
    doc = _envelope(_gitcrud(request), name)
    try:
        changed = library.set_metadata(
            doc, description=body.description, labels=body.labels, group=body.group
        )
    except library.InvalidArtifactError as exc:
        raise HTTPException(400, detail={"code": "invalid_metadata", "message": str(exc)}) from exc
    if not changed:
        return WriteResult(changed=False)
    protected = _enforce(request, user, name, doc)
    return _commit(
        request, user, name, doc, summary="edit metadata", protected=protected, if_match=if_match
    )


@router.delete("/{artifact}", response_model=WriteResult, dependencies=[_WRITE])
async def delete_artifact(artifact: str, user: CurrentUser, request: Request) -> WriteResult:
    """Remove an artefact. Refused while any instance links to it.

    Retiring an artefact that is still in use is a state change, not a delete, so
    the history behind a deployed file is never destroyed.
    """
    name = _check_name(artifact)
    gc = _gitcrud(request)
    _envelope(gc, name)
    linked = links.usage(gc, name)
    if linked:
        where = ", ".join(f"{u.service}/{u.instance}" for u in linked[:5])
        raise HTTPException(
            409,
            detail={
                "code": "artifact_in_use",
                "message": (
                    f"{name} is linked by {len(linked)} file(s) ({where}); unlink them "
                    "or set the artefact's state to disabled or deprecated"
                ),
            },
        )
    settings = request.app.state.settings
    scope, subject_summary = _fit_subject(name, "remove artefact")
    message = build_message(
        CommitContext(
            ctype="cfg",
            scope=scope,
            summary=subject_summary,
            actor=user.user_id,
            role=f"{_CLASS}:write",
        )
    )

    def _write(branch: str):
        return gc.delete(_CLASS, name, user.user_id, message=message, branch=branch)

    try:
        outcome = route_write(
            gc=gc,
            forge=_forge(request),
            environment=settings.env,
            mode=settings.gitops.mode,
            rbac_class=_CLASS,
            resource=f"{_CLASS}/{name}",
            actor=user.user_id,
            title=f"cfg({scope}): remove artefact",
            body=f"Remove library artefact {name} by {user.user_id}.",
            write=_write,
        )
    except ReviewRequiredError as exc:
        raise HTTPException(409, detail={"code": "review_required", "message": str(exc)}) from exc
    audit_resource_change(user.user_id, _CLASS, name, "deleted", {})
    return WriteResult(
        changed=outcome.changed,
        commit_sha=outcome.commit_sha,
        auto_merged=outcome.auto_merged,
        review_required=outcome.review_required,
        pr_url=outcome.pr_url,
    )


@router.put("/{artifact}/state", response_model=WriteResult, dependencies=[_WRITE])
async def set_state(
    artifact: str, body: StateRequest, user: CurrentUser, request: Request
) -> WriteResult:
    """Set the lifecycle state."""
    name = _check_name(artifact)
    doc = _envelope(_gitcrud(request), name)
    if not library.set_state(doc, _state(body.state)):
        return WriteResult(changed=False)
    protected = _enforce(request, user, name, doc)
    return _commit(request, user, name, doc, summary=f"state {body.state}", protected=protected)


# ── versions ──────────────────────────────────────────────────


@router.get("/{artifact}/versions", dependencies=[_READ])
async def list_versions(artifact: str, user: CurrentUser, request: Request) -> list[VersionSummary]:
    """Every version, oldest first, without their content."""
    name = _check_name(artifact)
    env = _envelope(_gitcrud(request), name)
    return [_version_summary(v) for v in library.versions(env)]


@router.post("/{artifact}/versions", response_model=WriteResult, dependencies=[_WRITE])
async def publish_version(
    artifact: str,
    body: PublishVersionRequest,
    user: CurrentUser,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> WriteResult:
    """Publish content as a version.

    Re-sending identical content is a no-op naming the version that already holds
    it; sending different content at an existing version number is refused.
    """
    name = _check_name(artifact)
    doc = _envelope(_gitcrud(request), name)
    try:
        artifact_kind = library.artifact_kind_of(doc)
    except library.UnknownKindError as exc:
        raise HTTPException(
            409,
            detail={
                "code": "unknown_kind",
                "message": f"artefact declares kind {exc.args[0]!r}, which the manifest no "
                "longer declares",
            },
        ) from None
    checked = _validate(request, artifact_kind.language, body.content)
    try:
        version, changed, plan = library.publish(
            doc,
            content=body.content,
            actor=user.user_id,
            description=body.description,
            message=body.message,
            version=body.version,
        )
    except library.InvalidArtifactError as exc:
        raise HTTPException(400, detail={"code": "invalid_content", "message": str(exc)}) from exc
    except VersionConflictError as exc:
        raise HTTPException(409, detail={"code": "version_conflict", "message": str(exc)}) from exc

    if not changed:
        return WriteResult(changed=False, version=version, validation=checked)
    protected = _enforce(request, user, name, doc)
    result = _commit(
        request,
        user,
        name,
        doc,
        summary=f"publish v{version}",
        protected=protected,
        if_match=if_match,
        plan=plan,
    )
    return result.model_copy(update={"version": version, "validation": checked})


@router.get("/{artifact}/versions/{version}", dependencies=[_READ])
async def get_version(
    artifact: str, version: int, user: CurrentUser, request: Request, response: Response
) -> VersionDetail:
    """One immutable version, with its digest as a strong ETag."""
    name = _check_name(artifact)
    gc = _gitcrud(request)
    env = _envelope(gc, name)
    found = _version(env, version)
    response.headers["ETag"] = f'"{found.digest}"'
    return VersionDetail(
        **_version_summary(found).model_dump(),
        kind=found.kind,
        encoding=str(found.encoding),
        content=_content(gc, name, env, found),
    )


@router.post("/{artifact}/rollback", response_model=WriteResult, dependencies=[_WRITE])
async def rollback(
    artifact: str, body: RollbackRequest, user: CurrentUser, request: Request
) -> WriteResult:
    """Point ``current`` back at an earlier version, keeping the newer ones."""
    name = _check_name(artifact)
    gc = _gitcrud(request)
    doc = _envelope(gc, name)
    found = _version(doc, body.version)
    # The stored bytes, not the API text form: rolling back rewrites the current
    # file, and a binary kind must land as itself.
    try:
        raw = gc.read_payload_bytes(_CLASS, name, found.path)
    except ResourceNotFoundError as exc:
        raise HTTPException(
            409,
            detail={
                "code": "missing_content",
                "message": f"version {body.version} names {found.path!r}, which is not in the repo",
            },
        ) from exc
    changed, plan = library.rollback(doc, body.version, raw)
    if not changed:
        return WriteResult(changed=False, version=body.version)
    protected = _enforce(request, user, name, doc)
    result = _commit(
        request,
        user,
        name,
        doc,
        summary=f"rollback to v{body.version}",
        protected=protected,
        plan=plan,
    )
    return result.model_copy(update={"version": body.version})


# ── tags ──────────────────────────────────────────────────────


@router.put("/{artifact}/tags/{tag}", response_model=WriteResult, dependencies=[_WRITE])
async def set_tag(
    artifact: str, tag: str, body: TagRequest, user: CurrentUser, request: Request
) -> WriteResult:
    """Point a tag at a version. Repointing is explicit, never a side effect."""
    name = _check_name(artifact)
    doc = _envelope(_gitcrud(request), name)
    _version(doc, body.version)
    try:
        changed = library.set_tag(doc, tag, body.version)
    except library.InvalidArtifactError as exc:
        raise HTTPException(400, detail={"code": "invalid_tag", "message": str(exc)}) from exc
    if not changed:
        return WriteResult(changed=False, version=body.version)
    protected = _enforce(request, user, name, doc)
    result = _commit(
        request, user, name, doc, summary=f"tag {tag} v{body.version}", protected=protected
    )
    return result.model_copy(update={"version": body.version})


@router.delete("/{artifact}/tags/{tag}", response_model=WriteResult, dependencies=[_WRITE])
async def delete_tag(artifact: str, tag: str, user: CurrentUser, request: Request) -> WriteResult:
    """Remove a tag. The versions it named are untouched."""
    name = _check_name(artifact)
    doc = _envelope(_gitcrud(request), name)
    try:
        library.delete_tag(doc, tag)
    except library.TagNotFoundError:
        raise HTTPException(
            404, detail={"code": "no_such_tag", "message": f"no tag {tag!r}"}
        ) from None
    protected = _enforce(request, user, name, doc)
    return _commit(request, user, name, doc, summary=f"untag {tag}", protected=protected)


# ── usage ─────────────────────────────────────────────────────


@router.get("/{artifact}/usage", dependencies=[_READ])
async def get_usage(artifact: str, user: CurrentUser, request: Request) -> list[UsageModel]:
    """Every instance file that links to this artefact."""
    name = _check_name(artifact)
    gc = _gitcrud(request)
    _envelope(gc, name)
    return [
        UsageModel(
            service=u.service,
            instance=u.instance,
            file_set=u.file_set,
            name=u.name,
            version=u.version,
            tag=u.tag,
        )
        for u in links.usage(gc, name)
    ]
