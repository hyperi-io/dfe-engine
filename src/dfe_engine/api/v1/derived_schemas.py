#  Project:      dfe-engine
#  File:         src/dfe_engine/api/v1/derived_schemas.py
#  Purpose:      REST API for derived schemas (column selections over a meta schema)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Derived-schema router -- create, read, update and delete a column selection.

Mounted ahead of the meta-schema routes so ``/definitions/derived/...`` is
answered here rather than by the ``{schema_path:path}`` catch-all.

Every write is checked against the base meta schema before it is stored, and a
failure names the column: a selection whose base does not define a name, an
index the type registry does not know, and an index the column's primitive
cannot take are three different 422s.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from dfe_engine.api.deps import CurrentUser, DerivedSchemaReg, SchemaReg, require_action
from dfe_engine.api.pagination import PaginatedResponse, PaginationParams, apply_search, apply_sort
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.git_identity import git_author
from dfe_engine.schema.derived import (
    DerivedSchema,
    DerivedSchemaError,
    DerivedSchemaVersion,
    IncompatibleIndexError,
    MalformedIndexError,
    UnknownIndexUseCaseError,
    UnknownSelectionError,
    validate_against_base,
)
from dfe_engine.schema.derived_registry import (
    DerivedSchemaNotFoundError,
    DerivedSchemaValidationError,
    canonical_derived_path,
    derived_reference,
)
from dfe_engine.source.type_registry import TypeRegistry

router = APIRouter(prefix="/schemas/definitions/derived", tags=["schemas"])


# -- Request / response models --------------------------------


class DerivedSchemaWriteRequest(BaseModel):
    """Create or replace a derived schema.

    ``path`` is optional and, when given, must equal the URL path -- the UI
    sends both and a disagreement between them is a bug worth reporting.
    """

    model_config = ConfigDict(extra="forbid")

    path: str | None = Field(
        default=None,
        description="Registry path (derived/<group>/<name>); must match the URL when set",
    )
    base: str = Field(
        ...,
        description="Meta-schema registry path, extensionless (e.g. meta/beats/filebeat)",
    )
    base_version: str = Field(..., description="Version of the base meta schema")
    current: str = Field(..., description="Version of this derived schema to make current")
    versions: dict[str, DerivedSchemaVersion] = Field(
        ..., min_length=1, description="Version id -> selection"
    )


class DerivedSchemaResponse(BaseModel):
    """A stored derived schema, as the API hands it back."""

    path: str = Field(..., description="Registry path (derived/<group>/<name>)")
    base: str = Field(..., description="Meta-schema registry path the columns come from")
    base_version: str = Field(..., description="Version of the base meta schema")
    current: str = Field(..., description="Current version of this derived schema")
    versions: dict[str, DerivedSchemaVersion] = Field(..., description="Version id -> selection")
    origin: Literal["deploy", "shipped"] = Field(
        ..., description="deploy when the deploy repo holds it, shipped when only the release does"
    )


class DerivedSchemaSummary(BaseModel):
    """One row of the derived-schema list."""

    path: str = Field(..., description="Registry path (derived/<group>/<name>)")
    base: str = Field(..., description="Meta-schema registry path the columns come from")
    base_version: str = Field(..., description="Version of the base meta schema")
    current: str = Field(..., description="Current version")
    versions: list[str] = Field(..., description="All version ids")
    column_count: int = Field(..., description="Columns the current version selects")
    updated_at: str = Field(..., description="Last write to the stored document")
    origin: Literal["deploy", "shipped"] = Field(
        ..., description="deploy when the deploy repo holds it, shipped when only the release does"
    )


def _response(schema: DerivedSchema, *, path: str) -> DerivedSchemaResponse:
    return DerivedSchemaResponse(
        path=derived_reference(path),
        base=schema.base,
        base_version=schema.base_version,
        current=schema.current,
        versions=schema.versions,
        origin=schema.origin,
    )


# -- Helpers --------------------------------------------------


def _validation_error(message: str, *, code: str = "validation_error") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"code": code, "message": message},
    )


def _canonical(schema_path: str) -> str:
    try:
        return canonical_derived_path(schema_path)
    except DerivedSchemaValidationError as exc:
        raise _validation_error(str(exc)) from exc


def _base_columns(base: str, base_version: str, schema_registry: SchemaReg) -> list[Any]:
    """The base meta schema's columns at the pinned version.

    A base that is absent, or a version it does not carry, is a 422 rather than
    a 404: the request is about the derived schema, and what it got wrong is the
    base it named.
    """
    from dfe_engine.schema.registry import (
        SchemaNotFoundError,
        SchemaValidationError,
        canonical_schema_path,
    )

    try:
        canonical = canonical_schema_path(base)
        meta = schema_registry.get_schema(canonical)
    except (SchemaNotFoundError, SchemaValidationError) as exc:
        raise _validation_error(
            f"base meta schema {base!r} is not in the registry", code="unknown_base"
        ) from exc

    version = meta.versions.get(base_version)
    if version is None:
        raise _validation_error(
            f"base meta schema {base!r} has no version {base_version!r}. "
            f"Defined: {', '.join(sorted(meta.versions))}",
            code="unknown_base_version",
        )
    return list(version.columns)


def _checked(
    body: DerivedSchemaWriteRequest, *, path: str, schema_registry: SchemaReg
) -> DerivedSchema:
    """Build the document the write stores, or raise the 422 that stops it."""
    if body.path is not None and _canonical(body.path) != path:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "path_mismatch",
                "message": f"Body path {body.path!r} must match URL path {path!r}",
            },
        )

    try:
        schema = DerivedSchema(
            base=body.base,
            base_version=body.base_version,
            current=body.current,
            versions=body.versions,
            path=derived_reference(path),
        )
    except ValueError as exc:
        raise _validation_error(str(exc)) from exc

    columns = _base_columns(schema.base, schema.base_version, schema_registry)
    registry = TypeRegistry.default()
    for version_id in schema.versions:
        try:
            validate_against_base(schema, columns, registry=registry, version_id=version_id)
        except UnknownSelectionError as exc:
            raise _validation_error(str(exc), code="unknown_column") from exc
        except UnknownIndexUseCaseError as exc:
            raise _validation_error(str(exc), code="unknown_index_use_case") from exc
        except IncompatibleIndexError as exc:
            raise _validation_error(str(exc), code="incompatible_index") from exc
        except MalformedIndexError as exc:
            raise _validation_error(str(exc), code="malformed_index") from exc
        except DerivedSchemaError as exc:
            raise _validation_error(str(exc)) from exc
    return schema


# -- Routes ---------------------------------------------------


@router.get(
    "",
    response_model=PaginatedResponse[DerivedSchemaSummary],
    dependencies=[Depends(require_action(scopes_dict["schema_read"]))],
)
async def list_derived_schemas(
    user: CurrentUser,
    registry: DerivedSchemaReg,
    pagination: PaginationParams = Depends(),
    search: str | None = Query(None, description="Search in path and base"),
    sort_by: str | None = Query(None, description="Sort field (path, base, current, updated_at)"),
    sort_order: str = Query("asc", description="Sort order: asc/desc"),
) -> PaginatedResponse[DerivedSchemaSummary]:
    """List every stored derived schema."""
    rows = apply_sort(apply_search(registry.list(), search, ["path", "base"]), sort_by, sort_order)
    return PaginatedResponse.from_list(
        [DerivedSchemaSummary(**row) for row in rows], pagination.page, pagination.per_page
    )


@router.get(
    "/{schema_path:path}",
    response_model=DerivedSchemaResponse,
    dependencies=[Depends(require_action(scopes_dict["schema_read"]))],
)
async def get_derived_schema(
    schema_path: str,
    user: CurrentUser,
    registry: DerivedSchemaReg,
) -> DerivedSchemaResponse:
    """Read one derived schema by registry path (e.g. ``beats/filebeat_auth``)."""
    path = _canonical(schema_path)
    try:
        schema = registry.get(path)
    except DerivedSchemaNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Derived schema {path!r} not found"},
        ) from exc
    except DerivedSchemaValidationError as exc:
        raise _validation_error(str(exc)) from exc
    return _response(schema, path=path)


@router.post(
    "/{schema_path:path}",
    response_model=DerivedSchemaResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_action(scopes_dict["schema_write"]))],
)
async def create_derived_schema(
    schema_path: str,
    body: DerivedSchemaWriteRequest,
    user: CurrentUser,
    registry: DerivedSchemaReg,
    schema_registry: SchemaReg,
) -> DerivedSchemaResponse:
    """Create a derived schema at the given registry path."""
    path = _canonical(schema_path)
    if registry.exists(path):
        raise _validation_error(f"A derived schema already exists at {path!r}")
    schema = _checked(body, path=path, schema_registry=schema_registry)
    saved = registry.save(
        schema,
        created_by=git_author(user),
        description=f"schema: create derived/{path}",
    )
    audit_resource_change(user.user_id, "derived_schema", path, "created")
    return _response(saved, path=path)


@router.put(
    "/{schema_path:path}",
    response_model=DerivedSchemaResponse,
    dependencies=[Depends(require_action(scopes_dict["schema_write"]))],
)
async def update_derived_schema(
    schema_path: str,
    body: DerivedSchemaWriteRequest,
    user: CurrentUser,
    registry: DerivedSchemaReg,
    schema_registry: SchemaReg,
) -> DerivedSchemaResponse:
    """Replace a derived schema. The body is the whole document, as on create."""
    path = _canonical(schema_path)
    if not registry.exists(path):
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Derived schema {path!r} not found"},
        )
    schema = _checked(body, path=path, schema_registry=schema_registry)
    saved = registry.save(
        schema,
        created_by=git_author(user),
        description=f"schema: update derived/{path}",
    )
    audit_resource_change(user.user_id, "derived_schema", path, "updated")
    return _response(saved, path=path)


@router.delete(
    "/{schema_path:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_action(scopes_dict["schema_delete"]))],
)
async def delete_derived_schema(
    schema_path: str,
    user: CurrentUser,
    registry: DerivedSchemaReg,
) -> None:
    """Delete a derived schema by registry path."""
    path = _canonical(schema_path)
    try:
        registry.delete(path, created_by=git_author(user))
    except DerivedSchemaNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Derived schema {path!r} not found"},
        ) from exc
    audit_resource_change(user.user_id, "derived_schema", path, "deleted")
