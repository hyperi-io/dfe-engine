"""Services router -- ServiceConfigRegistry CRUD.

GET    /api/v1/services                        -> Paginated list
GET    /api/v1/services/{service}/{instance}   -> Full config
PUT    /api/v1/services/{service}/{instance}   -> Save config
DELETE /api/v1/services/{service}/{instance}   -> Delete config
POST   /api/v1/services/{service}/{instance}/validate  -> Dry-run validate
GET    /api/v1/services/{service}/{instance}/history   -> Git history
POST   /api/v1/services/seed                   -> Seed built-in defaults
"""

import copy
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from scalo.api import ListQuery, Page, ProblemError, list_query, paginate, sort_by_order

from dfe_engine.api.conventions import apply_search
from dfe_engine.api.deps import CurrentUser, ServiceConfigReg, Settings, require_action
from dfe_engine.appmgmt import contract
from dfe_engine.appmgmt.appconfig import CONFIG_ROOT
from dfe_engine.auth.audit import audit_resource_change
from dfe_engine.auth.rbac_scopes import scopes_dict
from dfe_engine.services.registry import (
    ConfigNotFoundError,
    InvalidServiceConfigError,
    ServiceConfigRegistry,
    UnknownServiceError,
)
from dfe_engine.yaml_utils import deep_merge

router = APIRouter(prefix="/services", tags=["Services"])


def _shown(registry: ServiceConfigRegistry, service: str, config: dict[str, Any]) -> dict[str, Any]:
    """A config as a read shows it, every credential masked as :data:`contract.REDACTED`.

    Judged against the app's own schema, so a field it marks secret is hidden
    whatever it is called. With no readable contract the name rule judges alone.
    """
    try:
        found = registry.app_contract(service)
    except contract.ContractError:
        found = contract.AppContract(
            service=service, available=False, source=contract.ContractSource.ABSENT
        )
    return contract.redact_overlay(found, {CONFIG_ROOT: config})[CONFIG_ROOT]


def _unknown(exc: UnknownServiceError) -> ProblemError:
    """The refusal for a service the app manifest does not declare."""
    return ProblemError(404, str(exc), code="unknown_service")


def _unreadable(exc: contract.ContractError) -> ProblemError:
    """The failure for a contract mounted but unreadable, as the app contract routes answer it."""
    return ProblemError(500, str(exc), code="contract_unreadable")


# --- Response models ---


class ServiceConfigSummary(BaseModel):
    """One service config instance in a listing."""

    service: str
    instance: str
    updated_at: str | None = None


class ValidationResult(BaseModel):
    """Whether a service config is valid, and its errors."""

    valid: bool
    errors: list[str] = Field(default_factory=list)


class ServiceConfigDetail(BaseModel):
    """One service config as its instance runs it: the stored keys over the app's defaults."""

    service: str = Field(description="Service type (e.g. receiver, loader)")
    instance: str = Field(description="Instance name (e.g. production, staging)")
    config: dict[str, Any] = Field(description="Service-specific configuration")


class SeedResponse(BaseModel):
    """How many default service configs were seeded."""

    seeded: int


# --- Endpoints ---


ServiceConfigsList = Annotated[
    ListQuery,
    Depends(list_query(order_by=("service", "instance", "updated_at"), tiebreak="service")),
]


@router.get(
    "",
    response_model=Page[ServiceConfigSummary],
    dependencies=[Depends(require_action(scopes_dict["service_read"]))],
)
async def list_service_configs(
    user: CurrentUser,
    registry: ServiceConfigReg,
    query: ServiceConfigsList,
    service: str | None = Query(None, description="Filter by service type"),
    search: str | None = Query(None, description="Search in service/instance names"),
):
    """List service configs with optional filtering."""
    raw = registry.list_configs(service=service)
    raw = apply_search(raw, search, ["service", "instance"])
    raw = sort_by_order(raw, query.order_by)
    summaries = [
        ServiceConfigSummary(
            service=item.get("service", ""),
            instance=item.get("instance", "default"),
            updated_at=item.get("updated_at"),
        )
        for item in raw
    ]
    return paginate(summaries, query, bound={"service": service, "search": search})


@router.get(
    "/{service}/{instance}",
    response_model=ServiceConfigDetail,
    dependencies=[Depends(require_action(scopes_dict["service_read"]))],
)
async def get_service_config(
    service: str,
    instance: str,
    user: CurrentUser,
    registry: ServiceConfigReg,
) -> ServiceConfigDetail:
    """A service config as its instance runs it, every credential masked.

    The stored keys over the app's own defaults, read from its mounted contract.
    With no contract mounted only the stored keys are shown.
    """
    try:
        config = registry.effective_config(service, instance)
    except UnknownServiceError as exc:
        raise _unknown(exc) from exc
    except contract.ContractError as exc:
        raise _unreadable(exc) from exc
    except ConfigNotFoundError:
        raise ProblemError(
            404, f"Service config '{service}/{instance}' not found", code="not_found"
        )
    return ServiceConfigDetail(
        service=service, instance=instance, config=_shown(registry, service, config)
    )


@router.put(
    "/{service}/{instance}",
    dependencies=[Depends(require_action(scopes_dict["service_write"]))],
)
async def save_service_config(
    service: str,
    instance: str,
    body: dict[str, Any],
    user: CurrentUser,
    registry: ServiceConfigReg,
):
    """Create or update a service config.

    Each top-level section in the body replaces the stored one, and a section the
    body leaves out is kept as stored, so a form that sends some sections cannot
    drop the rest and the secrets in them. A section written as ``null`` is removed.

    A read shows every set secret masked, so a masked value written back keeps the
    secret stored there, matched as the app surface matches one. A mask with nothing
    stored behind it, or in a list entry that cannot be told apart, is a 400
    ``masked_value`` and nothing is saved. A changed field beside a masked secret,
    in the same mapping or with the secret one mapping down, is a 400
    ``credential_reentry_required``: the secret is typed again. A value the app's
    contract refuses is a 422 ``invalid_config``.
    """
    try:
        stored = registry.get_config(service, instance)
    except UnknownServiceError as exc:
        raise _unknown(exc) from exc
    except ConfigNotFoundError:
        stored = {}
    # A read shows the defaults beside the stored keys, so a write of that read is
    # restored against both.
    try:
        current = deep_merge(registry.defaults(service), copy.deepcopy(stored))
    except contract.ContractError as exc:
        raise _unreadable(exc) from exc
    shown = _shown(registry, service, current)
    sent = {key: value for key, value in body.items() if value is not None}
    try:
        restored = contract.restore_masked(sent, current, shown=shown)
    except contract.MaskedValueError as exc:
        raise ProblemError(400, str(exc), code=exc.code) from exc
    removed = {key for key, value in body.items() if value is None}
    config = {key: value for key, value in stored.items() if key not in removed} | restored
    try:
        registry.save_config(service=service, config=config, instance=instance)
    except InvalidServiceConfigError as exc:
        raise ProblemError(422, str(exc), code="invalid_config") from exc
    except contract.ContractError as exc:
        raise _unreadable(exc) from exc
    audit_resource_change(user.user_id, "service_config", f"{service}/{instance}", "updated")
    return {"service": service, "instance": instance, "message": "saved"}


@router.delete(
    "/{service}/{instance}",
    status_code=204,
    dependencies=[Depends(require_action(scopes_dict["service_delete"]))],
)
async def delete_service_config(
    service: str,
    instance: str,
    user: CurrentUser,
    registry: ServiceConfigReg,
):
    """Delete a service config."""
    # Verify existence before delete (delete_config is a no-op on missing)
    try:
        registry.get_config(service, instance)
    except UnknownServiceError as exc:
        raise _unknown(exc) from exc
    except ConfigNotFoundError:
        raise ProblemError(
            404, f"Service config '{service}/{instance}' not found", code="not_found"
        )
    registry.delete_config(service, instance)
    audit_resource_change(user.user_id, "service_config", f"{service}/{instance}", "deleted")


@router.post(
    "/{service}/{instance}/validate",
    response_model=ValidationResult,
    dependencies=[Depends(require_action(scopes_dict["service_validate"]))],
)
async def validate_service_config(
    service: str,
    instance: str,
    body: dict[str, Any],
    user: CurrentUser,
    registry: ServiceConfigReg,
):
    """Dry-run validate a service config without saving."""
    try:
        result = registry.validate(service, body)
    except contract.ContractError as exc:
        raise _unreadable(exc) from exc
    return ValidationResult(valid=result.valid, errors=result.errors)


@router.post(
    "/seed",
    response_model=SeedResponse,
    dependencies=[Depends(require_action(scopes_dict["service_write"]))],
)
async def seed_service_configs(user: CurrentUser, registry: ServiceConfigReg, settings: Settings):
    """Seed built-in default service configs. Non-destructive."""
    count = registry.seed_defaults(overwrite=False, settings=settings)
    audit_resource_change(user.user_id, "service_config", "all", "seeded")
    return SeedResponse(seeded=count)
